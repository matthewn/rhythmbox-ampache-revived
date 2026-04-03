#!/usr/bin/env python3

import os
import subprocess
import sys

from setuptools import setup
from setuptools.command.install import install


class PostInstall(install):
    """Compile GLib schemas after installing data files."""

    def run(self):
        super().run()
        if glib_compile_schemas:
            schemas_dir = os.path.join(
                self.install_base, 'share', 'glib-2.0', 'schemas')
            result = subprocess.run(
                ['glib-compile-schemas', schemas_dir], check=False)
            if result.returncode != 0:
                print(f"warning: glib-compile-schemas exited with code {result.returncode}")


glib_compile_schemas = True

if '--no-glib-compile-schemas' in sys.argv:
    glib_compile_schemas = False
    sys.argv = [a for a in sys.argv if a != '--no-glib-compile-schemas']


setup(
    name='rhythmbox-ampache-revived',
    cmdclass={'install': PostInstall},
    version='2026.1',
    description='A Rhythmbox plugin to stream music from an Ampache server',
    author='Matthew Newton',
    author_email='matthewn@mahnamahna.net',
    url='https://github.com/matthewn/rhythmbox-ampache-revived',
    packages=[],
    data_files=[
        ('lib/rhythmbox/plugins/ampache', [
            'ampache.plugin',
            'ampache.py',
            'AmpacheBrowser.py',
            'AmpacheConfigDialog.py',
        ]),
        ('share/rhythmbox/plugins/ampache', [
            'ampache-prefs.ui',
            'ampache.ico',
            'ampache.png',
        ]),
        ('share/glib-2.0/schemas', [
            'org.gnome.rhythmbox.plugins.ampache.gschema.xml',
        ]),
    ],
)
