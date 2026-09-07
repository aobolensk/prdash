import json
import re

import requests

from prdash.plugin_api import (
    GITHUB_API_VERSION,
    HEAD_SLOT,
    PLUGIN_API_VERSION,
    PluginJsonResponse,
    PluginMetadata,
    PluginNoContent,
    TemplateResource,
    UIContribution,
)


PACKAGE = 'prdash_github_actions_rerun_failed_jobs'
REPOSITORY_PART_PATTERN = re.compile(r'^[A-Za-z0-9_.-]+$')
RATE_LIMIT_STATUS_CODES = {403, 429}


class GitHubActionsRerunFailedJobsPlugin:
    metadata = PluginMetadata(
        plugin_id='github-actions-rerun-failed-jobs',
        name='GitHub Actions Re-run Failed Jobs',
        version='1.0.0',
        api_version=PLUGIN_API_VERSION,
        description='Re-run failed GitHub Actions jobs from pull request cards.',
    )

    def initialize(self, registrar):
        self.registrar = registrar
        registrar.register_ui(UIContribution(
            slot=HEAD_SLOT,
            template=TemplateResource(PACKAGE, 'templates/head.html'),
        ))
        registrar.register_ui(UIContribution(
            slot='pr_card.meta',
            template=TemplateResource(PACKAGE, 'templates/rerun_button.html'),
        ))
        registrar.register_route('rerun', self.rerun)
        registrar.register_route('rerun-track', self.rerun_track)
        registrar.register_route('update-branch', self.update_branch)

    def shutdown(self):
        pass

    def rerun(self, request, config):
        if request.method != 'POST':
            return PluginJsonResponse({'error': 'Method not allowed'}, status=405)

        outcome, message = self._handle_rerun(request)
        return self._toast(message, 'success' if outcome == 'success' else 'error')

    def rerun_track(self, request, config):
        if request.method != 'POST':
            return PluginJsonResponse({'error': 'Method not allowed'}, status=405)

        outcome, message = self._handle_rerun(request)
        return PluginJsonResponse({'outcome': outcome, 'message': message})

    def update_branch(self, request, config):
        if request.method != 'POST':
            return PluginJsonResponse({'error': 'Method not allowed'}, status=405)

        outcome, message = self._handle_update_branch(request)
        return PluginJsonResponse({'outcome': outcome, 'message': message})

    def _handle_update_branch(self, request):
        parsed, error = GitHubActionsRerunFailedJobsPlugin._parse_update_branch_request(request)
        if error:
            return 'error', error

        token = self.registrar.resolve_github_token(request.user_id)
        if not token:
            return 'error', 'GitHub authentication is unavailable.'

        owner, repository, pr_number = parsed
        return GitHubActionsRerunFailedJobsPlugin._attempt_update_branch(owner, repository, pr_number, token)

    @staticmethod
    def _parse_update_branch_request(request):
        owner, repository, error = GitHubActionsRerunFailedJobsPlugin._parse_owner_repository(request)
        pr_number = request.form_params.get('pr_number', '')
        if error or not pr_number.isdigit():
            return None, 'Could not determine the pull request to update.'
        return (owner, repository, pr_number), None

    @staticmethod
    def _attempt_update_branch(owner, repository, pr_number, token):
        headers = GitHubActionsRerunFailedJobsPlugin._api_headers(token)
        response = requests.put(
            f'https://api.github.com/repos/{owner}/{repository}/pulls/{pr_number}/update-branch',
            headers=headers,
            timeout=10,
        )
        if response.status_code != 202:
            message = GitHubActionsRerunFailedJobsPlugin._error_message(response)
            return GitHubActionsRerunFailedJobsPlugin._classify(response, message), message

        return 'success', 'Branch update requested.'

    def _handle_rerun(self, request):
        parsed, error = GitHubActionsRerunFailedJobsPlugin._parse_request(request)
        if error:
            return 'error', error

        token = self.registrar.resolve_github_token(request.user_id)
        if not token:
            return 'error', 'GitHub authentication is unavailable.'

        owner, repository, run_ids = parsed
        return GitHubActionsRerunFailedJobsPlugin._attempt_rerun(owner, repository, run_ids, token)

    @staticmethod
    def _parse_owner_repository(request):
        owner = request.form_params.get('owner', '')
        repository = request.form_params.get('repository', '')
        if not REPOSITORY_PART_PATTERN.fullmatch(owner) or not REPOSITORY_PART_PATTERN.fullmatch(repository):
            return None, None, 'Invalid repository.'
        return owner, repository, None

    @staticmethod
    def _parse_request(request):
        owner, repository, error = GitHubActionsRerunFailedJobsPlugin._parse_owner_repository(request)
        run_ids = list(request.form_lists.get('run_id', ()))
        if error or not run_ids or any(not run_id.isdigit() for run_id in run_ids):
            return None, 'Could not determine failed workflow runs.'
        return (owner, repository, run_ids), None

    @staticmethod
    def _api_headers(token):
        return {
            'Accept': 'application/vnd.github+json',
            'Authorization': f'Bearer {token}',
            'X-GitHub-Api-Version': GITHUB_API_VERSION,
        }

    @staticmethod
    def _attempt_rerun(owner, repository, run_ids, token):
        headers = GitHubActionsRerunFailedJobsPlugin._api_headers(token)
        for run_id in sorted(set(run_ids), key=int):
            response = requests.post(
                f'https://api.github.com/repos/{owner}/{repository}/actions/runs/{run_id}/rerun-failed-jobs',
                headers=headers,
                timeout=10,
            )
            if response.status_code != 201:
                message = GitHubActionsRerunFailedJobsPlugin._error_message(response)
                return GitHubActionsRerunFailedJobsPlugin._classify(response, message), message

        return 'success', f'Re-run requested for {len(set(run_ids))} workflow run(s).'

    @staticmethod
    def _classify(response, message):
        if response.status_code == 422:
            return 'retry'
        if GitHubActionsRerunFailedJobsPlugin._is_rate_limited(response, message):
            return 'retry'
        if 'already running' in message.lower():
            return 'retry'
        return 'error'

    @staticmethod
    def _is_rate_limited(response, message):
        if response.status_code not in RATE_LIMIT_STATUS_CODES:
            return False
        if response.headers.get('Retry-After'):
            return True
        if response.headers.get('X-RateLimit-Remaining') == '0':
            return True
        return 'rate limit' in message.lower()

    @staticmethod
    def _error_message(response):
        try:
            message = response.json().get('message')
        except ValueError:
            message = None
        return message or 'GitHub could not re-run the failed jobs.'

    @staticmethod
    def _toast(message, toast_type='error'):
        return PluginNoContent(headers={
            'HX-Trigger': json.dumps({
                'githubActionsRerunFailedJobsToast': {
                    'message': message,
                    'type': toast_type,
                },
            }),
        })


plugin = GitHubActionsRerunFailedJobsPlugin()
