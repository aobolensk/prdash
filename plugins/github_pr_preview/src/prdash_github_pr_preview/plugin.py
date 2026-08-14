import json
import re

import requests
from django.http import HttpResponse, HttpResponseNotAllowed

from dashboard.github_client import GITHUB_API_VERSION, GitHubClient
from prdash.plugin_api import (
    HEAD_SLOT,
    PLUGIN_API_VERSION,
    PR_CARD_ACTIONS_SLOT,
    PluginMetadata,
    PluginTemplateResponse,
    TemplateResource,
    UIContribution,
)


PACKAGE = 'prdash_github_pr_preview'
REPOSITORY_PART_PATTERN = re.compile(r'^[A-Za-z0-9_.-]+$')
GITHUB_API_URL = 'https://api.github.com'


class GitHubPRPreviewPlugin:
    metadata = PluginMetadata(
        plugin_id='github-pr-preview',
        name='GitHub PR Preview',
        version='1.0.0',
        api_version=PLUGIN_API_VERSION,
        description='Review pull request diffs and publish inline comments.',
    )

    def initialize(self, registrar):
        registrar.register_ui(UIContribution(
            slot=HEAD_SLOT,
            template=TemplateResource(PACKAGE, 'templates/head.html'),
        ))
        registrar.register_ui(UIContribution(
            slot=PR_CARD_ACTIONS_SLOT,
            template=TemplateResource(PACKAGE, 'templates/review_button.html'),
        ))
        registrar.register_route('preview', self.preview)
        registrar.register_route('comment', self.comment)

    def shutdown(self):
        pass

    @staticmethod
    def _request_values(request):
        values = request.POST if request.method == 'POST' else request.GET
        owner = values.get('owner', '')
        repository = values.get('repository', '')
        number = values.get('number', '')
        if (
            not REPOSITORY_PART_PATTERN.fullmatch(owner)
            or not REPOSITORY_PART_PATTERN.fullmatch(repository)
            or not number.isdigit()
            or int(number) < 1
        ):
            return None
        return owner, repository, int(number)

    @staticmethod
    def _headers(token):
        return {
            'Accept': 'application/vnd.github+json',
            'Authorization': f'Bearer {token}',
            'X-GitHub-Api-Version': GITHUB_API_VERSION,
        }

    @staticmethod
    def _error_message(response, fallback):
        try:
            message = response.json().get('message')
        except ValueError:
            message = None
        return message or fallback

    @staticmethod
    def _parse_patch(patch):
        lines = []
        old_line = new_line = None
        for source_line in patch.splitlines():
            if source_line.startswith('@@'):
                match = re.match(r'^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@', source_line)
                if match:
                    old_line, new_line = int(match.group(1)), int(match.group(2))
                lines.append({'kind': 'hunk', 'text': source_line})
                continue
            if source_line.startswith('+') and not source_line.startswith('+++'):
                lines.append({
                    'kind': 'addition',
                    'old_number': '',
                    'new_number': new_line,
                    'text': source_line[1:],
                    'line_number': new_line,
                    'side': 'RIGHT',
                })
                new_line = new_line + 1 if new_line is not None else None
                continue
            if source_line.startswith('-') and not source_line.startswith('---'):
                lines.append({
                    'kind': 'removal',
                    'old_number': old_line,
                    'new_number': '',
                    'text': source_line[1:],
                    'line_number': old_line,
                    'side': 'LEFT',
                })
                old_line = old_line + 1 if old_line is not None else None
                continue
            if source_line.startswith(' '):
                lines.append({
                    'kind': 'context',
                    'old_number': old_line,
                    'new_number': new_line,
                    'text': source_line[1:],
                    'line_number': new_line,
                    'side': 'RIGHT',
                })
                old_line = old_line + 1 if old_line is not None else None
                new_line = new_line + 1 if new_line is not None else None
                continue
            lines.append({'kind': 'meta', 'text': source_line})
        return lines

    def preview(self, request, config):
        if request.method != 'GET':
            return HttpResponseNotAllowed(['GET'])
        values = self._request_values(request)
        if values is None:
            return self._preview_error('Could not determine this pull request.')
        owner, repository, number = values
        token = GitHubClient(request.user)._get_token()
        if not token:
            return self._preview_error('GitHub authentication is unavailable.')

        headers = self._headers(token)
        pull_url = f'{GITHUB_API_URL}/repos/{owner}/{repository}/pulls/{number}'
        try:
            pull_response = requests.get(pull_url, headers=headers, timeout=15)
        except requests.exceptions.RequestException:
            return self._preview_error('GitHub could not load this pull request.')
        if pull_response.status_code != 200:
            return self._preview_error(
                self._error_message(pull_response, 'GitHub could not load this pull request.')
            )
        pull_request = pull_response.json()

        files = []
        page = 1
        while True:
            try:
                files_response = requests.get(
                    f'{pull_url}/files',
                    params={'per_page': 100, 'page': page},
                    headers=headers,
                    timeout=15,
                )
            except requests.exceptions.RequestException:
                return self._preview_error('GitHub could not load the changed files.')
            if files_response.status_code != 200:
                return self._preview_error(
                    self._error_message(files_response, 'GitHub could not load the changed files.')
                )
            page_files = files_response.json()
            for changed_file in page_files:
                files.append({
                    'filename': changed_file.get('filename', ''),
                    'status': changed_file.get('status', ''),
                    'additions': changed_file.get('additions', 0),
                    'deletions': changed_file.get('deletions', 0),
                    'lines': self._parse_patch(changed_file['patch'])
                    if changed_file.get('patch') else (),
                    'has_patch': bool(changed_file.get('patch')),
                })
            if len(page_files) < 100:
                break
            page += 1

        return PluginTemplateResponse(
            template=TemplateResource(PACKAGE, 'templates/preview.html'),
            context={
                'owner': owner,
                'repository': repository,
                'number': number,
                'pull_request': pull_request,
                'commit_id': pull_request.get('head', {}).get('sha', ''),
                'files': files,
            },
        )

    def comment(self, request, config):
        if request.method != 'POST':
            return HttpResponseNotAllowed(['POST'])
        values = self._request_values(request)
        path = request.POST.get('path', '')
        body = request.POST.get('body', '').strip()
        line = request.POST.get('line', '')
        side = request.POST.get('side', '')
        commit_id = request.POST.get('commit_id', '')
        if (
            values is None or not path or not body or not line.isdigit()
            or int(line) < 1 or side not in {'LEFT', 'RIGHT'} or not commit_id
        ):
            return self._toast('Enter a comment for a changed line.')
        owner, repository, number = values
        token = GitHubClient(request.user)._get_token()
        if not token:
            return self._toast('GitHub authentication is unavailable.')
        try:
            response = requests.post(
                f'{GITHUB_API_URL}/repos/{owner}/{repository}/pulls/{number}/comments',
                headers=self._headers(token),
                json={
                    'body': body,
                    'commit_id': commit_id,
                    'path': path,
                    'line': int(line),
                    'side': side,
                },
                timeout=15,
            )
        except requests.exceptions.RequestException:
            return self._toast('GitHub could not publish the inline comment.')
        if response.status_code != 201:
            return self._toast(
                self._error_message(response, 'GitHub could not publish the inline comment.')
            )
        return self._toast('Inline comment published.', 'success')

    @staticmethod
    def _preview_error(message):
        return PluginTemplateResponse(
            template=TemplateResource(PACKAGE, 'templates/preview.html'),
            context={'error': message},
        )

    @staticmethod
    def _toast(message, toast_type='error'):
        response = HttpResponse(status=204)
        response['HX-Trigger'] = json.dumps({
            'githubPRPreviewToast': {'message': message, 'type': toast_type},
        })
        return response


plugin = GitHubPRPreviewPlugin()
