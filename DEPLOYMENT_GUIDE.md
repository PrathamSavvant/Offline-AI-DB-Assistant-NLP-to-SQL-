# AI Database Assistant — offline Windows delivery

This is a self-contained application bundle, not a MySQL database dump. Keep
the folder structure unchanged. The bundled model is over 4 GB; use NTFS or
exFAT for a USB drive or destination volume.

1. Copy the entire folder to a location the user can write to, such as
   `C:\AI-Database-Assistant`. Do not place it under `Program Files` unless
   writable `project\knowledge`, `project\logs`, and `project\.runtime`
   locations have been arranged separately.
2. Copy `project\.env.example` to `project\.env`. Set `DB_HOST`, `DB_PORT`,
   `DB_NAME`, `DB_USER`, and `DB_PASSWORD` for the client's MySQL server.
   Use a read-only MySQL account. Never put real credentials in the archive.
3. Confirm MySQL is running and reachable. The app does not install MySQL or
   provide workshop data. The first schema scan may take several minutes.
4. Run `Install Desktop Shortcuts.ps1` once, then double-click **AI Database
   Assistant**. Allow the loading screen to finish. Use **Exit Application**
   in the sidebar when done; keep the Stop shortcut as a fallback.
5. Run the checks in `project\PRODUCTION_CHECKLIST.md` on the real database and
   the intended 8 GB CPU-only PC. Passing developer tests is not proof of
   correct answers on that database.

The application and model bind to `127.0.0.1`. They do not need internet
access. Keep the PC and MySQL account within your organization's access
controls. Review the Meta Llama 3.1 model licence and acceptable-use terms
before redistribution in the destination environment.
