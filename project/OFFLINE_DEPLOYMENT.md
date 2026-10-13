# Offline Windows deployment

This folder is self-contained. It uses the bundled Python runtime, installed
site-packages, llama.cpp executable, and GGUF model. Startup does not run pip,
Ollama, Docker, or any internet request.

On the client computer, copy `project/.env.example` to `project/.env` and set
the MySQL host, database name, read-only username, and password there. Do not
copy a developer PC's `.env` into the distribution. The database may be on the
same computer or an offline LAN. A live MySQL server and read-only account are
required; the bundle does not install MySQL or include the client's data.

On the target Windows computer:

1. Copy the entire delivered `AI-Database-Assistant-Offline-Windows` directory
   to a writable location on the target PC. Keep its internal folder layout.
   The model is larger than 4 GB, so use an NTFS or exFAT drive, not FAT32.
2. Run `Install Desktop Shortcuts.ps1` once if desktop shortcuts are wanted.
3. Double-click **AI Database Assistant** or `Start AI Database Assistant.vbs`.
4. Click **Exit Application** in the app's sidebar when finished. Confirming it
   stops the application and any local model started by this offline bundle.
   If the app was started manually, Exit stops only the app server; a separately
   started model must be stopped separately.
   Keep **Stop AI Database Assistant** as a backup if the browser is unavailable
   or startup has not completed. Closing a browser tab alone does not stop them.

The launcher opens a local loading page with model, knowledge, and application
stages. Model loading runs alongside knowledge preparation and application
startup. The page automatically opens the app once both the model and database
workspace are ready; failures show the relevant log location. Repeated icon
clicks reuse the startup in progress, and an already-ready app opens directly.

The schema cache is created only when missing or invalid. A first scan can take
several minutes for a large database. Logs, including elapsed startup milestones
in `startup-*.log`, are written under `project/logs`. The loading page uses a
temporary localhost port and closes its service after startup. The Stop shortcut
also stops any active startup coordinator and loading service. `-NoBrowser` on
`offline_launcher.ps1` runs the same startup without opening a browser.

The browser UI is deliberately bound to localhost. No external network access
is required. MySQL itself must already be running and reachable using the
values in `project/.env`.

Schema monitoring is background-only. It fingerprints the database, rebuilds knowledge in a staging directory when the fingerprint changes, validates that build, then promotes it and refreshes cached runtime context. If a build fails, the last active knowledge remains in use.
