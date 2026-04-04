# Rhythmbox Ampache Plugin (Revived!)

Got an [Ampache](https://ampache.org/) server? Still using [Rhythmbox](https://gitlab.gnome.org/GNOME/rhythmbox)? Well, get 'em talking to each other! This plugin will do the trick.

This is a **vibe-coded** evolution of lotan's [rhythmbox-ampache](https://github.com/lotan/rhythmbox-ampache) plugin, which itself is descended from a project originally hosted on [Google Code](http://code.google.com/p/rhythmbox-ampache) back in the dark ages.

Changes since lotan's last release in 2023:

* Plugin updated for compatibility with Rhythmbox 3.4.9.
* Fix some songs missing in the UI.
* Massively speed up fetching of remote library metadata.
* Massively speed up reading local cache of library metadata by using sqlite for storage instead of a flat XML file.
* Improve efficiency of metadata fetching when library is updated (fewer total re-fetches).

## Installation

```
make install
```

This copies the plugin files to `~/.local/share/rhythmbox/plugins/ampache/`, installs the GSettings schema to `~/.local/share/glib-2.0/schemas/`, and compiles it with `glib-compile-schemas`. To uninstall, run `make uninstall`.

## Usage

On first run, enable the plugin in Rhythmbox's Preferences dialog, then click the "Preferences" button to provide:

* Server URL
* Username
* Password

If the plugin doesn't work as intended, additional debug
information can be acquired by running Rhythmbox in debug mode:

```
rhythmbox -D ampache
```
