"""Shared navigation for public content pages; link targets are fixed."""

LINKS = (
    ('/', 'MoonBack'),
    ('/archive', 'Archive'),
    ('/journal', 'Journal'),
    ('/tools/', 'LUNA Tools'),
)


def render_navigation(current_path):
    links = []
    for path, label in LINKS:
        current = ''
        if current_path == path:
            current = ' aria-current="page"'
        elif path != '/' and current_path.startswith(path.rstrip('/') + '/'):
            current = ' aria-current="location"'
        links.append(f'<a{current} href="{path}">{label}</a>')
    return '<nav aria-label="Site">' + ' · '.join(links) + '</nav>'
