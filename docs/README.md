# hzforge docs

Documentation for `hzforge` — the tool that installs, uninstalls, diagnoses, and
repairs HUBzero Forge services (Subversion, Git, gitExternal, Trac) as
self-contained Apache drop-ins, independent of the m4 template and `hzcms`.

Read in roughly this order if new to the project:

1. **[summary.md](summary.md)** — what hzforge is, the four services, and the
   commands at a glance.
2. **[motivations.md](motivations.md)** — why it bypasses the m4/`hzcms`, the
   mod_python → mod_wsgi migration, and the one-file-per-service model.
3. **[architecture.md](architecture.md)** — how the drop-ins are wired:
   alias-vs-`<Location>` handlers and the `[END]` carve-out, the WSGI shim, the
   Trac handlers, and the restart-vs-reload logic.
4. **[services.md](services.md)** — the four services in detail (svn, git,
   gitExternal, trac): packages, repo dirs, and per-service config.
5. **[requirements.md](requirements.md)** — host, Python, packages/repos, and
   network prerequisites for running hzforge.
6. **[usage.md](usage.md)** — full command reference: install / uninstall /
   doctor / repair, options, exit codes, and safety flags.
7. **[migration.md](migration.md)** — mod_python → mod_wsgi runbook, the gotchas
   hzforge handles, and the Stage 2 (Python 3 / Trac 1.6) path.

For a quick CLI reference:

```
sudo python3 hzforge.py --help
```

---

The reference HUBzero deployment for this tool is a hub at Purdue; the same script
runs on other HUBzero hubs as well.
