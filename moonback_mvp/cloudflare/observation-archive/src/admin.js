// Authenticated read-only history. Never import response intake or generation.
export const TIME_KEY = `substr(time,1,19)||'.'||substr((CASE WHEN substr(time,20,1)='.' THEN replace(replace(substr(time,21),'+00:00',''),'Z','') ELSE '' END)||'000000',1,6)||'Z'`;
export const LIST_SQL = `SELECT event_id,time,method,status FROM observation_events
WHERE (${TIME_KEY},event_id) > (?1,?2)
ORDER BY ${TIME_KEY},event_id LIMIT ?3`;
export const SUMMARY_SQL = `SELECT COUNT(*) AS total,MIN(${TIME_KEY}) AS first_time,MAX(${TIME_KEY}) AS last_time FROM observation_events`;
export const DETAIL_SQL = `SELECT event_id,schema_version,time,path,method,status,response_completed,ua,agent_like FROM observation_events WHERE event_id = ?1`;
const MAX_BYTES = 64 * 1024;
const ID = /^obs-[0-9a-f]{32}$/;
const readBudgets = new WeakMap();

function response(value,status=200,head=false) {
  const body=JSON.stringify(value);
  const bytes=new TextEncoder().encode(body).length;
  if (bytes>MAX_BYTES) return response({error:'storage_unavailable'},503,head);
  return new Response(head?null:body,{status,headers:{
    'Content-Type':'application/json; charset=utf-8','Content-Length':String(bytes),
    'Cache-Control':'no-store','X-Content-Type-Options':'nosniff',
  }});
}
export function normalizedTime(value) {
  if (typeof value!=='string' || !/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|\+00:00)$/.test(value)
      || value.startsWith('0000')) throw new Error('invalid');
  const date=new Date(value);
  if (!Number.isFinite(date.getTime()) || date.toISOString().slice(0,19)!==value.slice(0,19)) throw new Error('invalid');
  const tail=value.replace(/(?:Z|\+00:00)$/,'').slice(19);
  return value.slice(0,19)+'.'+(tail.startsWith('.')?tail.slice(1):'').padEnd(6,'0')+'Z';
}
export function encodeCursor(parts) {
  return btoa(JSON.stringify(parts)).replaceAll('+','-').replaceAll('/','_').replace(/=+$/,'');
}
function decodeCursor(value) {
  if (typeof value!=='string' || !/^[A-Za-z0-9_-]{1,256}$/.test(value)) throw new Error('invalid');
  const parts=JSON.parse(atob(value.replaceAll('-','+').replaceAll('_','/')+'='.repeat((4-value.length%4)%4)));
  if (!Array.isArray(parts) || parts.length!==2 || normalizedTime(parts[0])!==parts[0]
      || typeof parts[1]!=='string' || !ID.test(parts[1]) || encodeCursor(parts)!==value) throw new Error('invalid');
  return parts;
}
function listQuery(url) {
  let limit=20,after=['0001-01-01T00:00:00.000000Z',''];
  const seen=new Set();
  if (url.search.length>512) throw new Error('invalid');
  if (url.search) for (const field of url.search.slice(1).split('&')) {
    const pair=field.split('=');
    if (pair.length!==2 || seen.has(pair[0])) throw new Error('invalid');
    seen.add(pair[0]);
    if (pair[0]==='limit' && /^[1-9][0-9]?$/.test(pair[1]) && Number(pair[1])<=50) limit=Number(pair[1]);
    else if (pair[0]==='cursor') after=decodeCursor(pair[1]);
    else throw new Error('invalid');
  }
  return {limit,after};
}
function summaryItem(row) {
  if (!row || typeof row.event_id!=='string' || !ID.test(row.event_id)
      || !['GET','HEAD'].includes(row.method)
      || !(row.status===null || (Number.isInteger(row.status)&&row.status>=200&&row.status<=599))) throw new Error('invalid');
  normalizedTime(row.time);
  return {event_id:row.event_id,time:row.time,method:row.method,status:row.status};
}
function detailItem(row) {
  const item=summaryItem(row);
  if (row.schema_version!==1) throw new Error('invalid');
  for (const [key,max] of [['path',2048],['ua',180]]) {
    if (typeof row[key]!=='string' || Array.from(row[key]).length>max || /[\uD800-\uDFFF]/u.test(row[key])) throw new Error('invalid');
  }
  if (!row.path.startsWith('/') || /[?#]/.test(row.path)) throw new Error('invalid');
  for (const key of ['response_completed','agent_like']) {
    if (![0,1,false,true].includes(row[key])) throw new Error('invalid');
  }
  return {...item,schema_version:1,path:row.path,ua:row.ua,
    response_completed:Boolean(row.response_completed),agent_like:Boolean(row.agent_like)};
}
function allowRead(db) {
  const now=Date.now(); let state=readBudgets.get(db);
  if (!state || now<state.start || now-state.start>=60000) state={start:now,count:0};
  if (state.count>=30) return false;
  state.count++; readBudgets.set(db,state); return true;
}
export async function handleAdmin(request,env,authenticate,collision) {
  const url=new URL(request.url);
  if (!url.pathname.startsWith('/v1/admin/observations')) return null;
  const head=request.method==='HEAD';
  const secret=env.MOONBACK_OBSERVATION_READ_SECRET;
  if (!['GET','HEAD'].includes(request.method) || collision
      || typeof secret!=='string' || secret.length>512
      || !await authenticate(request.headers.get('Authorization'),secret)) return response({error:'not_found'},404,head);
  let kind,ident,query;
  try {
    if (url.hash) throw new Error('invalid');
    if (url.pathname==='/v1/admin/observations') {kind='list';query=listQuery(url);}
    else if (url.pathname==='/v1/admin/observations/summary' && !url.search) kind='summary';
    else if (!url.search && url.pathname.startsWith('/v1/admin/observations/')) {
      ident=url.pathname.slice('/v1/admin/observations/'.length);
      if (!ID.test(ident)) throw new Error('invalid');
      kind='detail';
    } else throw new Error('invalid');
  } catch {return response({error:'invalid_request'},400,head);}
  try {
    if (!env.OBS_DB) throw new Error('binding');
    if (!allowRead(env.OBS_DB)) return response({error:'rate_limited'},429,head);
    if (kind==='summary') {
      const row=await env.OBS_DB.prepare(SUMMARY_SQL).first();
      if (!row || !Number.isSafeInteger(row.total) || row.total<0) throw new Error('invalid');
      if (row.total===0) {
        if (row.first_time!==null || row.last_time!==null) throw new Error('invalid');
      } else if (normalizedTime(row.first_time)!==row.first_time || normalizedTime(row.last_time)!==row.last_time
          || row.first_time>row.last_time) throw new Error('invalid');
      return response({total:row.total,first_time:row.first_time,last_time:row.last_time},200,head);
    }
    if (kind==='detail') {
      const row=await env.OBS_DB.prepare(DETAIL_SQL).bind(ident).first();
      if (!row) return response({error:'not_found'},404,head);
      const item=detailItem(row);
      if (item.event_id!==ident) throw new Error('invalid');
      return response(item,200,head);
    }
    const result=await env.OBS_DB.prepare(LIST_SQL).bind(...query.after,query.limit+1).all();
    if (!result.success || !Array.isArray(result.results) || result.results.length>query.limit+1) throw new Error('invalid');
    let prev=query.after; const seen=new Set();
    const items=result.results.map(row=>{
      const item=summaryItem(row),key=[normalizedTime(item.time),item.event_id];
      if (seen.has(item.event_id) || key[0]<prev[0] || (key[0]===prev[0]&&key[1]<=prev[1])) throw new Error('invalid');
      seen.add(item.event_id);prev=key;return item;
    });
    const more=items.length>query.limit;
    if (more) items.pop();
    const last=items.at(-1);
    return response({items,next_cursor:more?encodeCursor([normalizedTime(last.time),last.event_id]):null},200,head);
  } catch {return response({error:'storage_unavailable'},503,head);}
}
