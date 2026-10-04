"""Remove the Phase 1 tables left behind by swapping AUTH_USER_MODEL.

Phase 1 ran on ``django.contrib.auth.models.User``, so its ``auth_user``,
``auth_user_groups`` and ``auth_user_user_permissions`` tables already existed
in the database. Introducing ``accounts.User`` created the correct
``accounts_user*`` tables, but Django does not delete tables it no longer owns,
so the obsolete ones were left behind - still carrying foreign keys that point at
a user model the application no longer uses.

Leaving them is not harmless: they look like a second, working user store, and
they would mislead anyone inspecting the schema or writing a future migration.

Removal order matters. Permission rows for the old model reference its content
type, and group grants reference those permission rows, so each level has to go
before the one it depends on. Everything is guarded by an existence check, so the
migration is safe both on a database that came from Phase 1 and on a completely
fresh one.
"""

from django.db import migrations

# Child tables first: each references auth_user.
LEGACY_TABLES = (
    "auth_user_user_permissions",
    "auth_user_groups",
    "auth_user",
)


def drop_legacy_auth_user_tables(apps, schema_editor):
    connection = schema_editor.connection
    existing = set(connection.introspection.table_names())

    with connection.cursor() as cursor:
        if "django_content_type" in existing:
            cursor.execute(
                """
                DELETE FROM auth_group_permissions
                WHERE permission_id IN (
                    SELECT p.id
                    FROM auth_permission p
                    JOIN django_content_type ct ON ct.id = p.content_type_id
                    WHERE ct.app_label = 'auth' AND ct.model = 'user'
                )
                """
            )
            cursor.execute(
                """
                DELETE FROM auth_permission
                WHERE content_type_id IN (
                    SELECT id FROM django_content_type
                    WHERE app_label = 'auth' AND model = 'user'
                )
                """
            )
            # Only now is the content type unreferenced.
            cursor.execute(
                "DELETE FROM django_content_type "
                "WHERE app_label = 'auth' AND model = 'user'"
            )

        for table in LEGACY_TABLES:
            if table in existing:
                cursor.execute(f'DROP TABLE "{table}"')


def keep_legacy_tables(apps, schema_editor):
    """Not reversible.

    Recreating django.contrib.auth's default user model would re-introduce a
    second user store, which is precisely what this migration removes. Returning
    to Phase 1 means restoring that database from a backup, not reversing this
    migration.
    """


class Migration(migrations.Migration):
    dependencies = [
        ("accounts", "0001_initial"),
    ]

    operations = [
        migrations.RunPython(
            drop_legacy_auth_user_tables,
            reverse_code=keep_legacy_tables,
        ),
    ]