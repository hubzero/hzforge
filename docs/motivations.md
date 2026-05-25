# Motivations

## Off obsolete mod_python

HUBzero's per-tool Trac historically ran under **mod_python**, which is
unmaintained and a dead end. The forward path is **mod_wsgi**, in two stages:

| Stage | Handler | mod_wsgi | Python | Trac |
|-------|---------|----------|--------|------|
| **1** | mod_wsgi | 4.9.4 (pip-built) | 2.7 | existing (1.0.x) |
| **2** | mod_wsgi | 4.9.4 (`python3.11-mod_wsgi`) | 3.x | 1.6 |

Both stages use mod_wsgi 4.9.4, so the Apache config and the WSGI shim are
identical across the migration — only the `LoadModule` line and the interpreter
change. hzforge can deploy either handler (`--trac-handler mod_wsgi|mod_python`)
so a hub can move when ready. See [migration](../../operations/migration/).

## Drop-ins instead of the m4 template

The HUBzero vhost is generated from an m4 template. Putting Forge service
config there means:

- every change requires regenerating the vhost (and regenerating it is not always safe), and
- a future regeneration silently clobbers hand edits.

hzforge instead writes plain Apache config into `/etc/httpd/<hub>.conf.d/`, which
the vhost already includes. The config is **independent of the m4 vhost
lifecycle**: it survives vhost regeneration and needs no template toggles.

> The per-tool `svn.conf` / `git.conf` blocks are still produced by the hub's
> existing MySQL-driven generator. hzforge only *includes* them and shields them
> from the CMS catch-all rewrite — it does not reinvent that generator.

## One file per service

Each service is its own drop-in (`00-forge-svn.conf`, `00-forge-trac.conf`, …).
That keeps the lifecycle simple and isolated:

- `install <svc>` writes only that service's file;
- `uninstall <svc>` is just deleting that file (repository data is always kept);
- `doctor <svc>` / `repair <svc>` scope to one service, while genuinely global
  checks (`configtest`, interpreter state) always run.

## What it sets up

hzforge creates the per-service `/opt/<svc>/tools` directories with conventional
permissions, creates the `hzsvn`/`hzgit` groups, installs the required packages, and
writes the per-service drop-ins -- rather than regenerating the m4 vhost.
