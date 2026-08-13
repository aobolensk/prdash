import json
import re

import requests
from django.http import HttpResponse, HttpResponseNotAllowed

from dashboard.github_client import GITHUB_API_VERSION, GitHubClient
from prdash.plugin_api import (
    HEAD_SLOT,
    PLUGIN_API_VERSION,
    PluginMetadata,
    TemplateResource,
    UIContribution,
)


PACKAGE = 'prdash_github_actions_rerun_failed_jobs'
REPOSITORY_PART_PATTERN = re.compile(r'^[A-Za-z0-9_.-]+$')


class GitHubActionsRerunFailedJobsPlugin:
    metadata = PluginMetadata(
        plugin_id='github-actions-rerun-failed-jobs',
        name='GitHub Actions Re-run Failed Jobs',
        version='1.0.0',
        api_version=PLUGIN_API_VERSION,
        description='Re-run failed GitHub Actions jobs from pull request cards.',
    )

    def initialize(self, registrar):
        registrar.register_ui(UIContribution(
            slot=HEAD_SLOT,
            template=TemplateResource(PACKAGE, 'templates/head.html'),
        ))
        registrar.register_ui(UIContribution(
            slot='pr_card.meta',
            template=TemplateResource(PACKAGE, 'templates/rerun_button.html'),
        ))
        registrar.register_route('rerun', self.rerun)

    def shutdown(self):
        pass

    @staticmethod
    def rerun(request, config):
        if request.method != 'POST':
            return HttpResponseNotAllowed(['POST'])

        owner = request.POST.get('owner', '')
        repository = request.POST.get('repository', '')
        run_ids = request.POST.getlist('run_id')
        if (
            not REPOSITORY_PART_PATTERN.fullmatch(owner)
            or not REPOSITORY_PART_PATTERN.fullmatch(repository)
            or not run_ids
            or any(not run_id.isdigit() for run_id in run_ids)
        ):
            return GitHubActionsRerunFailedJobsPlugin._toast(
                'Could not determine failed workflow runs.'
            )

        token = GitHubClient(request.user)._get_token()
        if not token:
            return GitHubActionsRerunFailedJobsPlugin._toast('GitHub authentication is unavailable.')

        headers = {
            'Accept': 'application/vnd.github+json',
            'Authorization': f'Bearer {token}',
            'X-GitHub-Api-Version': GITHUB_API_VERSION,
        }
        for run_id in sorted(set(run_ids), key=int):
            response = requests.post(
                f'https://api.github.com/repos/{owner}/{repository}/actions/runs/{run_id}/rerun-failed-jobs',
                headers=headers,
                timeout=10,
            )
            if response.status_code != 201:
                return GitHubActionsRerunFailedJobsPlugin._toast(
                    GitHubActionsRerunFailedJobsPlugin._error_message(response)
                )

        return GitHubActionsRerunFailedJobsPlugin._toast(
            f'Re-run requested for {len(set(run_ids))} workflow run(s).',
            'success',
        )

    @staticmethod
    def _error_message(response):
        try:
            message = response.json().get('message')
        except ValueError:
            message = None
        return message or 'GitHub could not re-run the failed jobs.'

    @staticmethod
    def _toast(message, toast_type='error'):
        response = HttpResponse(status=204)
        response['HX-Trigger'] = json.dumps({
            'githubActionsRerunFailedJobsToast': {
                'message': message,
                'type': toast_type,
            },
        })
        return response


plugin = GitHubActionsRerunFailedJobsPlugin()
