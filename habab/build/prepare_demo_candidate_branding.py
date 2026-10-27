"""Install and verify brand assets referenced by the compiled demo entrypoint."""

import re
import sys
from pathlib import Path, PurePosixPath

from publish_build import install_brand_assets


BRAND_REFERENCE = re.compile(
    r'''(?:href|src|content)=["'](/static/habab/[^"'?#]+)["']''',
    re.IGNORECASE,
)


def prepare_demo_candidate_branding(dist):
    """Install the checked-in icons and fail if a local index reference is missing."""
    dist = Path(dist)
    if dist.is_symlink() or not dist.is_dir():
        raise ValueError('Compiled demo directory is missing or is a symlink')

    index = dist / 'index.html'
    if index.is_symlink() or not index.is_file():
        raise ValueError('Compiled demo index.html is missing')

    install_brand_assets(dist)
    html = index.read_text(encoding='utf-8')
    references = sorted(set(BRAND_REFERENCE.findall(html)))
    if not references:
        raise ValueError('Compiled demo index has no Habab brand asset references')

    for reference in references:
        relative = PurePosixPath(reference.lstrip('/'))
        if relative.is_absolute() or '..' in relative.parts:
            raise ValueError(f'Unsafe local brand asset reference: {reference}')
        asset = dist.joinpath(*relative.parts)
        if asset.is_symlink() or not asset.is_file():
            raise ValueError(f'Missing compiled Habab brand asset: {reference}')

    return references


def main():
    if len(sys.argv) != 2:
        raise SystemExit('usage: prepare_demo_candidate_branding.py DEMO_DIST')
    references = prepare_demo_candidate_branding(sys.argv[1])
    print(f'Installed Habab brand assets; verified {len(references)} index references')


if __name__ == '__main__':
    main()
