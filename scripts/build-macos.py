#!/usr/bin/env python3
"""Build the native macOS application with Python/Tk, Node, npm and pnpm."""

import argparse
import hashlib
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request

REPO = Path(__file__).resolve().parent.parent
APP = 'DSH-TM'
BUNDLE_ID = 'io.github.arnen7000.dsh-tm'
MIN_MACOS = '13.5'
NODE_VERSION = '24.21.0'
NODE_SHA256 = {
    'arm64': '6239d4cf92d864487ec8cd3615038f7b67e7f58b77b21cd2f09ea9fbd68065fe',
    'x64': '0ae5a24c24bb7d015cd816c5036b3f90f2945aa872fcf54e58da054753b3a299',
}
PNPM_VERSION = '10.34.5'
PNPM_SHA512 = (
    'a4ee05f2f73658255bd6a89859c065a45c28a57daefae2c893a168ee2b73168c37'
    'b91e83e57ea67654ad03f03031746430e8bce38e362e042605fb8abc80192e'
)


def extract_download(url, checksum, algorithm, work, downloads):
    name = url.rsplit('/', 1)[1]
    archive = work / name
    cached = downloads / name if downloads else None
    if cached and cached.is_file():
        shutil.copyfile(cached, archive)
    else:
        print(f'Downloading {url}', flush=True)
        with urllib.request.urlopen(url, timeout=120) as source, archive.open('wb') as dest:
            shutil.copyfileobj(source, dest)
    with archive.open('rb') as source:
        if hashlib.file_digest(source, algorithm).hexdigest() != checksum:
            raise ValueError(f'{algorithm} checksum mismatch for {name}')
    with tarfile.open(archive) as source:
        source.extractall(work, filter='data')


def stage_runtime(arch, work, downloads):
    node_name = f'node-v{NODE_VERSION}-darwin-{arch}'
    extract_download(f'https://nodejs.org/dist/v{NODE_VERSION}/{node_name}.tar.xz',
                     NODE_SHA256[arch], 'sha256', work, downloads)
    extract_download(f'https://registry.npmjs.org/pnpm/-/pnpm-{PNPM_VERSION}.tgz',
                     PNPM_SHA512, 'sha512', work, downloads)
    source = work / node_name
    runtime = work / 'node'
    (runtime / 'bin').mkdir(parents=True)
    shutil.copy2(source / 'bin' / 'node', runtime / 'bin' / 'node')
    shutil.copy2(source / 'LICENSE', runtime / 'LICENSE')
    modules = runtime / 'lib' / 'node_modules'
    shutil.copytree(source / 'lib' / 'node_modules' / 'npm', modules / 'npm')
    pnpm = modules / 'pnpm'
    shutil.move(work / 'package', pnpm)
    # Keep the platform's reflink addon and discard the other bundled native programs.
    for binary in (pnpm / 'dist').rglob('*.node'):
        if not binary.name.startswith(f'reflink.darwin-{arch}-'):
            binary.unlink()
    for binary in (pnpm / 'dist' / 'vendor').glob('*.exe'):
        binary.unlink()
    for command, entry in {
        'npm': 'npm/bin/npm-cli.js',
        'npx': 'npm/bin/npx-cli.js',
        'pnpm': 'pnpm/bin/pnpm.cjs',
    }.items():
        launcher = runtime / 'bin' / command
        launcher.write_text(
            '#!/bin/sh\n'
            'bin_dir="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -L)" || exit 1\n'
            'export PATH="$bin_dir${PATH:+:$PATH}"\n'
            f'exec "$bin_dir/node" "$bin_dir/../lib/node_modules/{entry}" "$@"\n',
            encoding='utf-8',
        )
        launcher.chmod(0o755)
    return runtime


def write_spec(work, runtime, arch):
    icon = work / f'{APP}.icns'
    subprocess.run(['/usr/bin/sips', '-s', 'format', 'icns',
                    REPO / 'assets' / 'dsh-icon.ico', '--out', icon], check=True)
    version = (REPO / 'VERSION').read_text(encoding='utf-8-sig').strip()
    if len(version.split('.')) != 3 or not all(x.isdecimal() for x in version.split('.')):
        raise ValueError(f'Expected numeric app version in VERSION, found {version!r}')
    files = (
        'VERSION', 'LICENSE', 'dsh_manager.pyw', 'dsh-tm.py', 'config/config.example.json',
        'config/cordis.patch.yml.template', 'lang/en.json', 'scripts/check-env.py',
        'docs/dsh管理器使用说明.md', 'docs/user-guide.en.md', 'docs/macos.md',
        'docs/架构与性能.md', 'docs/agent-api.md',
        'assets/dsh-icon.ico', 'assets/dsh-icon-violet.ico',
    )
    datas = [(str(REPO / name), str(Path(name).parent)) for name in files]
    datas.extend((str(runtime / name), f'runtime/node/{Path(name).parent}') for name in
                 ('bin/npm', 'bin/npx', 'bin/pnpm', 'LICENSE'))
    datas.append((str(runtime / 'lib'), 'runtime/node/lib'))
    binaries = [(str(runtime / 'bin' / 'node'), 'runtime/node/bin')]
    info = {
        'CFBundleVersion': os.environ.get('GITHUB_RUN_NUMBER', '1'),
        'CFBundleName': APP,
        'CFBundleDisplayName': APP,
        'LSMinimumSystemVersion': MIN_MACOS,
        'NSHighResolutionCapable': True,
    }
    spec = work / f'{APP}.spec'
    spec.write_text(f'''a = Analysis(
    [{str(REPO / 'dsh_manager.pyw')!r}], pathex=[{str(REPO)!r}],
    binaries={binaries!r}, datas={datas!r},
    hiddenimports=['argparse', 'ast', 'glob', 'platform', 'queue'],
)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, console=False,
          name={APP!r}, target_arch={arch!r}, upx=False)
coll = COLLECT(exe, a.binaries, a.datas, name={APP!r}, upx=False)
app = BUNDLE(coll, name={APP + '.app'!r}, icon={str(icon)!r},
             bundle_identifier={BUNDLE_ID!r}, version={version!r}, info_plist={info!r})
''', encoding='utf-8')
    return spec


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--arch', choices=NODE_SHA256, required=True)
    parser.add_argument('--output', type=Path, required=True, help='destination directory')
    parser.add_argument('--downloads', type=Path, help='directory with pre-downloaded verified archives')
    args = parser.parse_args()
    native_arch = {'arm64': 'arm64', 'x86_64': 'x64'}.get(platform.machine())
    if sys.platform != 'darwin' or args.arch != native_arch:
        parser.error('run with native macOS Python for the requested architecture')
    import tkinter
    import PyInstaller

    print(f'Python {platform.python_version()}, Tk {tkinter.TkVersion}, PyInstaller {PyInstaller.__version__}', flush=True)
    app = args.output.resolve() / f'{APP}.app'
    if app.exists():
        parser.error(f'{app} already exists')
    args.output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='dsh-tm-build-', dir=args.output) as temp:
        work = Path(temp).resolve()
        runtime = stage_runtime(args.arch, work, args.downloads)
        spec = write_spec(work, runtime, 'arm64' if args.arch == 'arm64' else 'x86_64')
        env = dict(os.environ, PYINSTALLER_CONFIG_DIR=str(work / 'cache'),
                   MACOSX_DEPLOYMENT_TARGET=MIN_MACOS)
        dist = work / 'dist'
        subprocess.run([sys.executable, '-m', 'PyInstaller', '--noconfirm',
                        '--distpath', dist, '--workpath', work / 'build', spec],
                       env=env, cwd=REPO, check=True)
        bundle = dist / app.name
        subprocess.run(['/usr/bin/codesign', '--verify', '--deep', '--strict', bundle], check=True)
        bundle.rename(app)
    print(app)


if __name__ == '__main__':
    main()
