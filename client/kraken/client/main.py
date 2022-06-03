# Copyright 2022 The Kraken Authors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import sys
import json
import logging
import getpass
import urllib.parse

import click
import requests
from tabulate import tabulate

from . import version as client_version
from . import toolops

log = logging.getLogger(__name__)


class Session:
    def __init__(self, base_url):
        u = urllib.parse.urlparse(base_url)
        port = u.port
        if not port:
            port = ''
        else:
            port = ':%d' % port
        self.base_url = '%s://%s%s/api' % (u.scheme, u.hostname, port)

        self.auth_token = None
        self.user = 'admin'
        if u.username:
            self.user = u.username
        if u.password:
            self.password = u.password
        else:
            self.password = getpass.getpass('Enter password for %s user: ' % self.user)

    def _initial_headers(self):
        h = {}
        if self.auth_token is None:
            return h
        h['Authorization'] = 'Bearer %s' % self.auth_token
        return h

    def get(self, url, exp_status=None, **kwargs):
        url = self.base_url + url
        headers = self._initial_headers()
        resp = requests.request("GET", url, params=kwargs, headers=headers)
        log.debug('GET: %s', resp.json())
        if exp_status is None:
            assert resp.status_code == 200
        else:
            assert resp.status_code == exp_status
        return resp

    def post(self, url, payload=None, payload_type=False, exp_status=None):
        url = self.base_url + url
        headers = self._initial_headers()

        if payload_type == 'binary':
            resp = requests.request("POST", url, data=payload, headers=headers)
        elif payload_type == 'files':
            resp = requests.request("POST", url, files=payload, headers=headers)
        else:
            resp = requests.request("POST", url, json=payload, headers=headers)

        log.debug('POST: %s', resp.json())
        if exp_status is None:
            assert resp.status_code in [200, 201]
        else:
            assert resp.status_code == exp_status
        return resp

    def patch(self, url, payload=None, exp_status=None):
        url = self.base_url + url
        headers = self._initial_headers()
        resp = requests.request("PATCH", url, json=payload, headers=headers)
        log.debug('PATCH: %s', resp.json())
        if exp_status is None:
            assert resp.status_code in [200, 201]
        else:
            assert resp.status_code == exp_status
        return resp

    def login(self):
        payload = {"user": self.user, "password": self.password}
        resp = self.post('/sessions', payload)
        data = resp.json()
        self.auth_token = data['token']
        return resp


def _make_session(server):
    if not server:
        log.info("Bad server URL: '%s'", server)
        sys.exit(1)
    s = Session(server)
    resp = s.login()
    # TODO check resp

    resp = s.get('/version')
    data = resp.json()
    if data['version'] != client_version.version:
        log.info('Version mismatch, Kraken Server: %s, kkci client: %s', data['version'], client_version.version)
        log.info('Please, install proper version of kkci to match Kraken Server version')
        sys.exit(1)
    return s


@click.group()
@click.option('-v', '--verbose', count=True, help='Increase verbosity.')
@click.option('-s', '--server', default='', envvar='KRAKEN_SERVER_ADDR', help='Kraken Server URL')
@click.pass_context
def main(ctx, verbose, server):
    'Kraken Client'
    level = logging.INFO
    if verbose > 0:
        level = logging.DEBUG
    logging.basicConfig(format='%(message)s', level=level)

    ctx.ensure_object(dict)
    ctx.obj['server'] = server


@main.command('version')
def version_():
    'Show kkci, Kraken client, version.'
    log.info(client_version.version)


@main.command()
@click.pass_context
def server_version(ctx):
    'Show Kraken server version.'
    s = _make_session(ctx.obj['server'])

    resp = s.get('/version')
    data = resp.json()
    log.info(data['version'])


@main.group('tools')
@click.pass_context
def tools_cmd(ctx):
    'Manage Kraken Tools'


@tools_cmd.command('list')
@click.pass_context
def list_(ctx):
    'List registered Kraken Tools'
    s = _make_session(ctx.obj['server'])

    resp = s.get('/tools')
    data = resp.json()

    tools = []
    for t in data['items']:
        tools.append(dict(id=t['id'],
                          name=t['name'],
                          location=t['location'],
                          entry=t['entry'],
                          version=t['version']))
    log.info(tabulate(tools, headers={'id': 'Id',
                                      'name': 'Name',
                                      'location': 'Location',
                                      'entry': 'Entry',
                                      'version': 'Version'}))


@tools_cmd.command()
@click.option('-r', '--version', default=False, is_flag=False,
              help='Overwrite existing tool version. "latest" version overwrites the latest tool version. If not provided then new version is created.')
@click.argument('tool-file')
@click.pass_context
def register(ctx, version, tool_file):
    'Register a new or overwrite existing tool described in indicated TOOL_FILE.'

    # load file and parse as JSON
    with open(tool_file) as fp:
        data = json.load(fp)

    if version:
        data['version'] = version

    s = _make_session(ctx.obj['server'])
    resp = s.post('/tools', data)
    data = resp.json()
    log.info("Stored tool %s@%s", data['name'], data['version'])


@tools_cmd.command()
@click.option('-t', '--tag', default='main', help='Tag or branch to pull Git repo.')
@click.option('-f', '--tool-file', default='tool.json', help='Path to JSON file with tool metadata within Git repo.')
@click.argument('url')
@click.pass_context
def register_remote(ctx, url, tag, tool_file):
    'Register a tool located in remote repository indicated by URL.'

    data = dict(url=url, tag=tag, tool_file=tool_file)

    s = _make_session(ctx.obj['server'])
    resp = s.post('/tools', data)
    data = resp.json()
    if 'name' in data:
        log.info("Stored tool %s@%s", data['name'], data['version'])
    else:
        log.info("Stored tool under id %d", data['id'])


@tools_cmd.command()
@click.option('-r', '--version', default=False, is_flag=False,
              help='Overwrite existing tool version. "latest" version overwrites the latest tool version. If not provided then new version is created.')
@click.argument('tool-file')
@click.pass_context
def upload(ctx, version, tool_file):
    """Upload tool's code from DIRECTORY to Kraken Server.

    By default new tool version is created (the last version is incremented by 1).
    """

    s = _make_session(ctx.obj['server'])

    meta, tf, files_num = toolops.package_tool(tool_file)

    try:
        log.debug('Packed %d files to %s', files_num, tf.name)

        if version:
            meta['version'] = version

        meta_str = json.dumps(meta)

        with open(tf.name, "rb") as tbf:

            send_data = {
                'meta': (None, meta_str, "application/json; charset=UTF-8"),
                'file': ('tool.zip', tbf, "application/zip"),
            }

            url = '/tools/%s/zip' % meta['name']
            s.post(url, send_data, payload_type='files')

    finally:
        tf.close()


@main.command()
@click.argument('out-file')
@click.pass_context
def dump_workflow_schema(ctx, out_file):
    s = _make_session(ctx.obj['server'])
    resp = s.get('/workflow-schema')
    with open(out_file, "w") as fp:
        json.dump(resp.json(), fp)



if __name__ == '__main__':
    main()  # pylint: disable=no-value-for-parameter
