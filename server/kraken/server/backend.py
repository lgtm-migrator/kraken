# Copyright 2020 The Kraken Authors
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

import os
import json
import time
import logging
import datetime

from flask import request
from sqlalchemy import or_
from sqlalchemy.orm import joinedload
from sqlalchemy.orm.attributes import flag_modified
from sqlalchemy.exc import IntegrityError
from psycopg2.errors import UniqueViolation
import giturlparse
import minio

from .models import db, Job, Step, Agent, TestCase, TestCaseResult, Issue, Secret, Artifact, File
from .models import System
from . import consts
from .bg import jobs as bg_jobs
from .. import version

log = logging.getLogger(__name__)

JOB = {
    "name": "build-tarball",
    "id": 123,
    "steps": [{
        "tool": "git",
        "checkout": "git@gitlab.isc.org:isc-projects/kea.git",
        "branch": "master"
    }, {
        "tool": "shell",
        "cmd": "cd kea && autoreconf -fi && ./configure && make dist"
    }, {
        "tool": "artifacts",
        "type": "file",
        "upload": "aaa-{ver}.tar.gz"
    }]
}


def _left_time(job):
    now = datetime.datetime.utcnow()
    slip = now - job.assigned
    timeout = job.timeout
    timeout2 = timeout - slip.total_seconds()
    # reduce slightly timeout
    timeout3 = timeout2 * 0.9
    log.info('%s-%s-%s: now: %s, slip:%s, to1: %s, to2: %s, to3: %s',
             job.name, job.system, job.agents_group_id,
             now, slip, timeout, timeout2, timeout3)
    return int(timeout3)


def _get_or_create_minio_bucket(job):
    bucket_name = '%08d' % job.run.flow.branch_id

    minio_addr = os.environ.get('KRAKEN_MINIO_ADDR', consts.DEFAULT_MINIO_ADDR)
    access_key = os.environ['MINIO_ACCESS_KEY']
    secret_key = os.environ['MINIO_SECRET_KEY']
    mc = minio.Minio(minio_addr, access_key=access_key, secret_key=secret_key, secure=False)
    found = mc.bucket_exists(bucket_name)
    if not found:
        mc.make_bucket(bucket_name)

    return bucket_name


def _handle_get_job(agent):
    if agent.job is None:
        return {'job': {}}

    # handle canceling situation
    if agent.job.state == consts.JOB_STATE_COMPLETED:
        job = agent.job
        agent.job = None
        db.session.commit()
        log.info("unassigned canceled job %s from %s", job, agent)
        return {'job': {}}

    job = agent.job.get_json()

    job['timeout'] = _left_time(agent.job)

    # prepare test list for execution
    tests = []
    for tcr in agent.job.results:
        tests.append(tcr.test_case.name)
    if tests:
        job['steps'][-1]['tests'] = tests

    # attach trigger data to job
    if agent.job.run.flow.trigger_data:
        job['trigger_data'] = agent.job.run.flow.trigger_data

    # attach storage info to job
    job['flow_id'] = agent.job.run.flow_id
    job['run_id'] = agent.job.run_id

    minio_bucket = _get_or_create_minio_bucket(agent.job)

    # prepare steps
    project = agent.job.run.flow.branch.project
    for step in job['steps']:
        # insert secret from ssh-key
        if 'ssh-key' in step:
            value = step['ssh-key']
            secret = Secret.query.filter_by(project=project, name=value).one_or_none()
            if secret is None:
                raise Exception("Secret '%s' does not exists in project %s" % (value, project.id))
            step['ssh-key'] = dict(username=secret.data['username'],
                                   key=secret.data['key'])

        # insert secret from access-token
        if 'access-token' in step:
            value = step['access-token']
            secret = Secret.query.filter_by(project=project, name=value).one_or_none()
            if secret is None:
                raise Exception("Secret '%s' does not exists in project %s" % (value, project.id))
            step['access-token'] = secret.data['secret']

        # add http url to git
        if step['tool'] == 'git':
            url = step['checkout']
            url = giturlparse.parse(url)
            if url.valid:
                url = url.url2https
                step['http_url'] = url
            else:
                log.info('invalid git url %s', step['checkout'])

        if step['tool'] == 'artifacts':
            minio_addr = os.environ.get('KRAKEN_MINIO_ADDR', consts.DEFAULT_MINIO_ADDR)
            step['minio_addr'] = minio_addr
            step['minio_bucket'] = minio_bucket
            step['minio_access_key'] = os.environ['MINIO_ACCESS_KEY']
            step['minio_secret_key'] = os.environ['MINIO_SECRET_KEY']
            if 'destination' not in step:
                step['destination'] = '.'

    if not agent.job.started:
        agent.job.started = datetime.datetime.utcnow()
        agent.job.state = consts.JOB_STATE_ASSIGNED
        db.session.commit()

    return {'job': job}


def _store_results(job, step, result):
    t0 = time.time()
    q = TestCaseResult.query.filter_by(job=job)
    q = q.options(joinedload('test_case'))
    q = q.join('test_case')
    or_list = []
    results = {}
    for tr in result['test-results']:
        or_list.append(TestCase.name == tr['test'])
        results[tr['test']] = tr
    q = q.filter(or_(*or_list))

    # update status of existing test case results
    cnt = 0
    for tcr in q.all():
        tr = results.pop(tcr.test_case.name)
        tcr.cmd_line = tr['cmd']
        tcr.result = tr['status']
        tcr.values = tr['values'] if 'values' in tr else None
        cnt += 1
    db.session.commit()
    t1 = time.time()
    log.info('reporting %s existing test records took %ss', cnt, (t1 - t0))

    # create test case results if they didnt exist
    tool_test_cases = {}
    q = TestCase.query.filter_by(tool=step.tool)
    for tc in q.all():
        tool_test_cases[tc.name] = tc
    for tc_name, tr in results.items():
        tc = tool_test_cases.get(tc_name, None)
        if tc is None:
            tc = TestCase(name=tc_name, tool=step.tool)
        tcr = TestCaseResult(test_case=tc, job=step.job,
                             cmd_line=tr['cmd'],
                             result=tr['status'],
                             values=tr['values'] if 'values' in tr else None)
    db.session.commit()
    t2 = time.time()
    log.info('reporting %s new test records took %ss', len(results), (t2 - t1))


def _store_issues(job, result):
    t0 = time.time()
    for issue in result['issues']:
        issue_type = 0
        if issue['type'] in consts.ISSUE_TYPES_CODE:
            issue_type = consts.ISSUE_TYPES_CODE[issue['type']]
        else:
            log.warning('unknown issue type: %s', issue['type'])
        extra = {}
        for k, v in issue.items():
            if k not in ['line', 'column', 'path', 'symbol', 'message']:
                extra[k] = v
        Issue(issue_type=issue_type, line=issue['line'], column=issue['column'], path=issue['path'], symbol=issue['symbol'],
              message=issue['message'][:511], extra=extra, job=job)
    db.session.commit()
    t1 = time.time()
    log.info('reporting %s issues took %ss', len(result['issues']), (t1 - t0))


def _store_artifacts(job, step):
    t0 = time.time()
    flow = job.run.flow
    if not flow.artifacts:
        flow.artifacts = dict(public=dict(size=0, count=0),
                              private=dict(size=0, count=0),
                              report=dict(size=0, count=0, entries=[]))
    if not flow.artifacts_files:
        flow.artifacts_files = []

    run = job.run
    if not run.artifacts:
        run.artifacts = dict(public=dict(size=0, count=0),
                             private=dict(size=0, count=0),
                             report=dict(size=0, count=0, entries=[]))
    if not run.artifacts_files:
        run.artifacts_files = []

    action = step.fields.get('action', 'upload')
    public = step.fields.get('public', False)
    if action == 'report':
        section = 'report'
        section_id = consts.ARTIFACTS_SECTION_REPORT
    elif public:
        section = 'public'
        section_id = consts.ARTIFACTS_SECTION_PUBLIC
    else:
        section = 'private'
        section_id = consts.ARTIFACTS_SECTION_PRIVATE

    for artifact in step.result['artifacts']:
        flow.artifacts[section]['size'] += artifact['size']
        flow.artifacts[section]['count'] += 1

        run.artifacts[section]['size'] += artifact['size']
        run.artifacts[section]['count'] += 1

        if section == 'report':
            report_entry = artifact.get('report_entry', None)
            if report_entry:
                flow.artifacts['report']['entries'].append(report_entry)
                run.artifacts['report']['entries'].append(report_entry)

        path = artifact['path']
        f = File.query.filter_by(path=path).one_or_none()
        if f is None:
            f = File(path=path)
        Artifact(file=f, flow=flow, run=run, size=artifact['size'], section=section_id)

    flag_modified(flow, 'artifacts')
    flag_modified(run, 'artifacts')
    db.session.commit()

    t1 = time.time()
    log.info('reporting %s artifacts took %ss', len(step.result['artifacts']), (t1 - t0))


def _handle_step_result(agent, req):
    response = {}
    if agent.job is None:
        log.error('job in agent %s is missing, reporting some old job %s, step %s',
                  agent, req['job_id'], req['step_idx'])
        return response

    if agent.job_id != req['job_id']:
        log.error('agent %s is reporting some other job %s',
                  agent, req['job_id'])
        return response

    try:
        result = req['result']
        step_idx = req['step_idx']
        status = result['status']
        del result['status']
        if status not in list(consts.STEP_STATUS_TO_INT.keys()):
            raise ValueError("unknown status: %s" % status)
    except:
        log.exception('problems with parsing request')
        return response

    job = agent.job
    step = job.steps[step_idx]
    step.result = result
    step.status = consts.STEP_STATUS_TO_INT[status]

    # handle canceling situation
    if job.state == consts.JOB_STATE_COMPLETED:
        agent.job = None
        db.session.commit()
        log.info("canceling job %s on %s", job, agent)
        response['cancel'] = True
        return response

    db.session.commit()

    # store test results
    if 'test-results' in result:
        _store_results(job, step, result)

    # store issues
    if 'issues' in result:
        _store_issues(job, result)

    # store issues
    if 'artifacts' in result:
        _store_artifacts(job, step)

    # check if all steps are done so job is finised
    job_finished = True
    log.info('checking steps')
    for s in job.steps:
        log.info('%s: %s', s.index, consts.STEP_STATUS_NAME[s.status]
                 if s.status in consts.STEP_STATUS_NAME else s.status)
        if s.status == consts.STEP_STATUS_DONE:
            continue
        if s.status == consts.STEP_STATUS_ERROR:
            job_finished = True
            break
        job_finished = False
        break
    if job_finished:
        job.state = consts.JOB_STATE_EXECUTING_FINISHED
        job.finished = datetime.datetime.utcnow()
        agent.job = None
        db.session.commit()
        t = bg_jobs.job_completed.delay(job.id)
        log.info('job %s finished by %s, bg processing: %s', job, agent, t)
    else:
        response['timeout'] = _left_time(job)

    return response


def _create_test_records(step, tests):
    t0 = time.time()
    tool_test_cases = {}
    q = TestCase.query.filter_by(tool=step.tool)
    for tc in q.all():
        tool_test_cases[tc.name] = tc

    for t in tests:
        tc = tool_test_cases.get(t, None)
        if tc is None:
            tc = TestCase(name=t, tool=step.tool)
        TestCaseResult(test_case=tc, job=step.job)
    db.session.commit()
    t1 = time.time()
    log.info('creating %s test records took %ss', len(tests), (t1 - t0))


def _handle_dispatch_tests(agent, req):
    if agent.job is None:
        log.error('job in agent %s is missing, reporting some old job %s, step %s',
                  agent, req['job_id'], req['step_idx'])
        return {}

    if agent.job_id != req['job_id']:
        log.error('agent %s is reporting some other job %s',
                  agent, req['job_id'])
        return {}

    job = agent.job

    # handle canceling situation
    if job.state == consts.JOB_STATE_COMPLETED:
        agent.job = None
        db.session.commit()
        log.info("canceling job %s on %s", job, agent)
        return {'cancel': True}

    try:
        tests = req['tests']
        step_idx = req['step_idx']
        step = job.steps[step_idx]
    except:
        log.exception('problems with parsing request')
        return {}

    tests_cnt = len(tests)

    if len(set(tests)) != tests_cnt:
        log.warning('there are tests duplicates')
        return {}

    if tests_cnt == 0:
        # TODO
        raise NotImplementedError

    if tests_cnt == 1:
        _create_test_records(step, tests)
        db.session.commit()
        return {'tests': tests}

    # simple dispatching: divide to 2 jobs, current and new one
    part = tests_cnt // 2
    part1 = tests[:part]
    part2 = tests[part:]

    _create_test_records(step, part1)
    db.session.commit()

    # new timeout reduced by nearly a half
    timeout = int(job.timeout * 0.6)
    if timeout < 60:
        timeout = 60

    # create new job and its steps
    job2 = Job(run=job.run, name=job.name, agents_group=job.agents_group, system=job.system, timeout=timeout)
    for s in job.steps:
        s2 = Step(job=job2, index=s.index, tool=s.tool, fields=s.fields.copy())
        if s.index == step_idx:
            _create_test_records(s2, part2)
    db.session.commit()

    return {'tests': part1}


def _handle_host_info(agent, req):  # pylint: disable=unused-argument
    log.info('HOST INFO %s', req['info'])
    agent.host_info = req['info']
    db.session.commit()

    system = req['info']['system']
    try:
        System(name=system, executor='local')
        db.session.commit()
    except IntegrityError as e:
        if not isinstance(e.orig, UniqueViolation):
            log.exception('IGNORED')


def _handle_keep_alive(agent, req):
    job = agent.job

    # handle canceling situation
    if job and job.state == consts.JOB_STATE_COMPLETED:
        agent.job = None
        db.session.commit()
        log.info("canceling job %s on %s", job, agent)
        return {'cancel': True}

    return {}


def _handle_unknown_agent(address, ip_address):
    Agent(name=address, address=address, authorized=False, ip_address=ip_address, last_seen=datetime.datetime.utcnow())
    db.session.commit()


def serve_agent_request():
    req = request.get_json()
    # log.info('request headers: %s', request.headers)
    # log.info('request remote_addr: %s', request.remote_addr)
    # log.info('request args: %s', request.args)
    log.info('request data: %s', str(req)[:200])

    msg = req['msg']
    address = req['address']
    if address is None:
        address = request.remote_addr
    # log.info('agent address: %s', address)

    agent = Agent.query.filter_by(address=address).one_or_none()
    if agent is None:
        log.warning('unknown agent %s', address)
        _handle_unknown_agent(address, request.remote_addr)
        return json.dumps({})

    agent.last_seen = datetime.datetime.utcnow()
    agent.deleted = None
    db.session.commit()

    if not agent.authorized:
        log.warning('unauthorized agent %s from %s', address, request.remote_addr)
        return json.dumps({'unauthorized': True})

    response = {}

    if msg == consts.AGENT_MSG_GET_JOB:
        response = _handle_get_job(agent)

        clickhouse_addr = os.environ.get('KRAKEN_CLICKHOUSE_ADDR', consts.DEFAULT_CLICKHOUSE_ADDR)
        response['cfg'] = dict(clickhouse_addr=clickhouse_addr)
        response['version'] = version.version

    elif msg == consts.AGENT_MSG_STEP_RESULT:
        response = _handle_step_result(agent, req)

    elif msg == consts.AGENT_MSG_DISPATCH_TESTS:
        response = _handle_dispatch_tests(agent, req)

    elif msg == consts.AGENT_MSG_HOST_INFO:
        _handle_host_info(agent, req)
        response = {}

    elif msg == consts.AGENT_MSG_KEEP_ALIVE:
        response = _handle_keep_alive(agent, req)

    else:
        log.warning('unknown msg: %s', msg)
        response = {}

    log.info('sending response: %s', str(response)[:200])
    return json.dumps(response)
