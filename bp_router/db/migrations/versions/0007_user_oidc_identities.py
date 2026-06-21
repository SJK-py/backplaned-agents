"""user_oidc_identities — external OIDC subjects linked to a user.

SSO login resolves a validated `(issuer, sub)` to a local `user_id`. Kept in
a child table (not on `users`) so one account can carry a password AND any
number of linked OPs — and so the OIDC subject (PII) is scrubbed on purge.

`PRIMARY KEY (issuer, sub)` enforces "one OP identity ↔ exactly one account"
(`sub` is only unique per issuer); the `user_id` index powers the reverse
"list / unlink my logins" lookup. Structurally mirrors the suite's
`(platform, external_id) → user_id` channel mapping.
"""

from __future__ import annotations

from alembic import op

revision = "0007_user_oidc_identities"
down_revision = "0006_web_signup_password"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE user_oidc_identities (
            issuer        text NOT NULL,
            sub           text NOT NULL,
            user_id       text NOT NULL
                          REFERENCES users(user_id)
                          ON UPDATE CASCADE ON DELETE CASCADE,
            email_at_link text,
            created_at    timestamptz NOT NULL DEFAULT now(),
            last_login_at timestamptz,
            PRIMARY KEY (issuer, sub)
        )
        """
    )
    op.execute(
        "CREATE INDEX user_oidc_identities_user_id_idx "
        "ON user_oidc_identities (user_id)"
    )


def downgrade() -> None:
    op.execute("DROP TABLE user_oidc_identities")
