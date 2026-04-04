PLUGIN_DIR  = $(HOME)/.local/share/rhythmbox/plugins/ampache
SCHEMA_DIR  = $(HOME)/.local/share/glib-2.0/schemas
SCHEMA_FILE = org.gnome.rhythmbox.plugins.ampache.gschema.xml

PLUGIN_FILES = \
	ampache.plugin \
	ampache.py \
	AmpacheBrowser.py \
	AmpacheConfigDialog.py \
	ampache-prefs.ui \
	ampache.ico \
	ampache.png

.PHONY: install uninstall

install:
	install -d $(PLUGIN_DIR)
	install -m 644 $(PLUGIN_FILES) $(PLUGIN_DIR)/
	install -d $(SCHEMA_DIR)
	install -m 644 $(SCHEMA_FILE) $(SCHEMA_DIR)/
	glib-compile-schemas $(SCHEMA_DIR)

uninstall:
	rm -rf $(PLUGIN_DIR)
	rm -f $(SCHEMA_DIR)/$(SCHEMA_FILE)
	glib-compile-schemas $(SCHEMA_DIR)
