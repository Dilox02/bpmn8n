# Data safety (PLAN G6, G8): the audit log, task runs kept for retries, LLM provider keys encrypted at rest

import django.core.serializers.json
from django.db import migrations, models

import bpmn8n.core.crypto


def convert(schema_editor, change):
    """Rewrites every stored API key with `change`, on the raw column (the field would encrypt again)."""
    table, column = (schema_editor.quote_name(n) for n in ("bpmn8n_llmprovider", "api_key"))
    with schema_editor.connection.cursor() as cursor:
        cursor.execute(f"SELECT id, {column} FROM {table}")  # noqa: S608 (names quoted, no input)
        for pk, value in cursor.fetchall():
            if value and (new := change(value)) != value:
                cursor.execute(f"UPDATE {table} SET {column} = %s WHERE id = %s", [new, pk])  # noqa: S608


def encrypt_keys(apps, schema_editor):
    convert(schema_editor, bpmn8n.core.crypto.encrypt)


def decrypt_keys(apps, schema_editor):
    convert(schema_editor, bpmn8n.core.crypto.decrypt)


class Migration(migrations.Migration):

    dependencies = [
        ('bpmn8n', '0013_people'),
    ]

    operations = [
        migrations.CreateModel(
            name='AuditEntry',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('at', models.DateTimeField(auto_now_add=True, db_index=True)),
                ('actor', models.CharField(max_length=150)),
                ('action', models.CharField(db_index=True, max_length=50)),
                ('object_type', models.CharField(blank=True, max_length=50)),
                ('object_id', models.CharField(blank=True, max_length=200)),
                ('case_id', models.CharField(blank=True, db_index=True, max_length=100)),
                ('details', models.JSONField(blank=True, default=dict, encoder=django.core.serializers.json.DjangoJSONEncoder)),
                ('ip', models.GenericIPAddressField(blank=True, help_text="the client's, when there was a request", null=True)),
            ],
            options={
                'verbose_name_plural': 'audit entries',
                'ordering': ('-at', '-id'),
            },
        ),
        migrations.CreateModel(
            name='TaskRun',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('task', models.CharField(max_length=200)),
                ('case_id', models.CharField(max_length=100)),
                ('node_id', models.CharField(max_length=200)),
                ('run_index', models.PositiveIntegerField()),
                ('item_index', models.PositiveIntegerField()),
                ('status', models.CharField(choices=[('running', 'running'), ('done', 'done')], default='running', max_length=10)),
                ('response', models.JSONField(blank=True, encoder=django.core.serializers.json.DjangoJSONEncoder, null=True)),
                ('created', models.DateTimeField(auto_now_add=True)),
                ('started', models.DateTimeField(help_text='when the call that holds it began')),
            ],
            options={
                'indexes': [models.Index(fields=['case_id'], name='bpmn8n_task_case_id_bf0b39_idx')],
                'constraints': [models.UniqueConstraint(fields=('task', 'case_id', 'node_id', 'run_index', 'item_index'), name='unique_task_run')],
            },
        ),
        migrations.AlterField(
            model_name='llmprovider',
            name='api_key',
            field=bpmn8n.core.crypto.EncryptedText(blank=True, help_text='Empty for local models; stored encrypted'),
        ),
        migrations.RunPython(encrypt_keys, decrypt_keys),
    ]
