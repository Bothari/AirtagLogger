"""
Break-glass recovery, for when single sign-on is broken and nobody can sign in
(password sign-in is refused while SSO is enabled). Needs shell access:

    docker exec airtag-logger python -m app.recover disable-sso

This turns SSO off, so password sign-in works again for accounts that have a
password. Re-enable SSO in Settings > General once it's fixed.
"""

import sqlite3
import sys

from app.database import DB_PATH


def main(argv: list[str]) -> int:
    if argv != ["disable-sso"]:
        print(__doc__.strip())
        return 2
    conn = sqlite3.connect(DB_PATH)
    conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('oidc_enabled', '0')")
    conn.commit()
    admins = [r[0] for r in conn.execute(
        "SELECT username FROM users WHERE role = 'admin' AND password_hash IS NOT NULL")]
    print("SSO disabled; password sign-in is on.")
    print("Admins who can sign in with a password:", ", ".join(admins) or "none (see below)")
    if not admins:
        print("No admin has a password. Set one with:\n"
              "  docker exec -it airtag-logger python -c \"import sqlite3,bcrypt,getpass; "
              "c=sqlite3.connect('" + DB_PATH + "'); "
              "c.execute('UPDATE users SET password_hash=? WHERE username=?', "
              "(bcrypt.hashpw(getpass.getpass().encode(), bcrypt.gensalt()).decode(), input('username: '))); c.commit()\"")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
