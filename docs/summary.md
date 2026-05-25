# Summary

`hzforge` installs, uninstalls, diagnoses, and repairs **HUBzero Forge
services** — Subversion, Git, gitExternal, and Trac — as self-contained Apache
**drop-ins**, independent of the m4 vhost template.

On a HUBzero hub each tool gets a project area under `/tools/<name>/…`:

| Service       | URL space                          | Apache mechanism |
|---------------|------------------------------------|------------------|
| `svn`         | `/tools/<name>/svn`                | `mod_dav_svn` (`<Location>`) |
| `git`         | `/tools/<name>/git/<name>`         | `git-http-backend` (`ScriptAliasMatch`) |
| `gitExternal` | `/tools/<name>/gitExternal/<name>` | `git-http-backend` (`ScriptAliasMatch`) |
| `trac`        | `/tools/<name>/{wiki,timeline,browser,ticket,…}` | mod_wsgi (default) or mod_python |

hzforge writes **one config file per service** at
`/etc/httpd/<hub>.conf.d/00-forge-<svc>.conf`, picked up by the vhost's existing
`IncludeOptional <hub>.conf.d/*.conf` — so it never edits the m4-generated vhost
or requires regenerating the vhost.

## Commands at a glance

```
sudo python3 hzforge.py install                 # all services
sudo python3 hzforge.py install trac            # one service (positional)
sudo python3 hzforge.py install svn git trac
sudo python3 hzforge.py uninstall git           # stop serving git (data kept)
sudo python3 hzforge.py doctor                  # diagnose (exit 1 on FAIL)
sudo python3 hzforge.py doctor git              # diagnose one service
sudo python3 hzforge.py repair                  # fix drift
```

Preview any command with `--dry-run`; nothing touches the running server until
`apachectl configtest` passes.

See **[motivations](../motivations/)** for why it bypasses the m4 vhost,
**[architecture](../../reference/architecture/)** for how the drop-ins are wired,
**[services](../../reference/services/)** for the four services, and
**[usage](../../operations/usage/)** for the full command reference.
