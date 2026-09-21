#!/usr/bin/env python3

import configparser
import os
from pathlib import Path
import re
import subprocess


def main():
    root = Path(__file__).resolve().parents[2]

    def git(*args):
        return subprocess.check_output(['git', '-C', str(root), *args], text=True).strip()

    if git('rev-parse', '--is-shallow-repository') != 'false':
        raise RuntimeError('Nightly versions require the full Git history')

    config = configparser.ConfigParser()
    config.read(root / '.bumpversion.cfg')
    base = config['bumpversion']['current_version']
    count = git('rev-list', '--count', 'HEAD')
    commit = git('rev-parse', 'HEAD')
    short = git('rev-parse', '--short=12', 'HEAD')
    version = f'{base}+git.{count}.g{short}'
    pattern = re.compile(r'(?<![\w.+-])' + re.escape(base) + r'(?![\w.+-])')
    changes = {}

    for section in config.sections():
        if not section.startswith('bumpversion:file:'):
            continue
        path = root / section.removeprefix('bumpversion:file:')
        original = path.read_text()
        updated, replacements = pattern.subn(version, original)
        if not replacements and version not in original:
            raise RuntimeError(f'Base version not found in {path.relative_to(root)}')
        changes[path] = updated

    changelog = root / 'debian/changelog'
    original = changelog.read_text()
    if not original.startswith(f'openrazer ({version}) '):
        date = git('show', '-s', '--format=%cD', 'HEAD')
        changes[changelog] = (
            f'openrazer ({version}) unstable; urgency=low\n\n'
            f'  * Nightly build from commit {commit}.\n\n'
            ' -- GitHub Actions <41898282+github-actions[bot]@users.noreply.github.com>'
            f'  {date}\n\n{original}'
        )

    for path, content in changes.items():
        path.write_text(content)

    if 'GITHUB_OUTPUT' in os.environ:
        with open(os.environ['GITHUB_OUTPUT'], 'a') as output:
            output.write(f'version={version}\nshort_sha={short}\n')
    print(version)


if __name__ == '__main__':
    main()
