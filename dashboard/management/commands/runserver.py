from django.contrib.staticfiles.management.commands.runserver import Command as StaticFilesRunserverCommand
from django.core.management import call_command
from django.core.management.base import CommandError


class Command(StaticFilesRunserverCommand):
    def add_arguments(self, parser):
        super().add_arguments(parser)
        parser.add_argument(
            "--skip-migration-check",
            action="store_true",
            help="Start the server even if unapplied migrations exist.",
        )

    def handle(self, *args, **options):
        self.skip_migration_check = options["skip_migration_check"]
        self.migrations_checked = False
        super().handle(*args, **options)

    def run(self, **options):
        self.check_migrations()
        super().run(**options)

    def check_migrations(self):
        if self.skip_migration_check or self.migrations_checked:
            return

        try:
            call_command("migrate", check=True, verbosity=0)
        except SystemExit as exc:
            if exc.code:
                raise CommandError(
                    "Unapplied migrations exist. Run 'python manage.py migrate' or "
                    "use 'runserver --skip-migration-check' to bypass this check."
                ) from exc
            raise
        self.migrations_checked = True
