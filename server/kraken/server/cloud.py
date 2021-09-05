# Copyright 2021 The Kraken Authors
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

import time
import base64
import random
import logging

# AWS
import boto3
import botocore

# AZURE
import azure
from azure.identity import ClientSecretCredential
from azure.mgmt.resource import ResourceManagementClient
from azure.mgmt.network import NetworkManagementClient
from azure.mgmt.network.models import SecurityRuleAccess, SecurityRuleDirection, SecurityRuleProtocol
from azure.mgmt.compute import ComputeManagementClient
from azure.mgmt.subscription import SubscriptionClient

import requests
from sqlalchemy.orm.attributes import flag_modified

from .models import db
from .models import Agent, AgentAssignment, get_setting
from . import utils

log = logging.getLogger(__name__)



# AWS EC2 ###################################################################

def check_aws_settings():
    access_key = get_setting('cloud', 'aws_access_key')
    if not access_key:
        return 'AWS access key is empty'
    if len(access_key) < 16:
        return 'AWS access key is too short'
    if len(access_key) > 128:
        return 'AWS access key is too long'

    secret_access_key = get_setting('cloud', 'aws_secret_access_key')
    if not secret_access_key:
        return 'AWS secret access key is empty'
    if len(secret_access_key) < 36:
        return 'AWS secret access key is too short'

    try:
        ec2 = boto3.client('ec2', region_name='us-east-1', aws_access_key_id=access_key, aws_secret_access_key=secret_access_key)
        ec2.describe_regions()
    except Exception as ex:
        return str(ex)

    return 'ok'


def create_ec2_vms(aws, access_key, secret_access_key,
                   ag, system, num,
                   server_url, minio_addr, clickhouse_addr):
    region = aws['region']
    ec2 = boto3.client("ec2", region_name=region, aws_access_key_id=access_key, aws_secret_access_key=secret_access_key)
    ec2_res = boto3.resource('ec2', region_name=region, aws_access_key_id=access_key, aws_secret_access_key=secret_access_key)

    # get key pair
    if not ag.extra_attrs:
        ag.extra_attrs = {}
    if 'aws' not in ag.extra_attrs:
        ag.extra_attrs['aws'] = {}
    if 'key-pair' not in ag.extra_attrs['aws']:
        key_name = 'kraken-%s' % ag.name
        try:
            key_pair = ec2.create_key_pair(KeyName=key_name)
        except botocore.exceptions.ClientError as ex:
            if ex.response['Error']['Code'] == 'InvalidKeyPair.Duplicate':
                ec2.delete_key_pair(KeyName=key_name)
                key_pair = ec2.create_key_pair(KeyName=key_name)
            else:
                log.exception('problem with creating AWS key pair')
                return
        ag.extra_attrs['aws']['key-pair'] = key_pair
        flag_modified(ag, 'extra_attrs')
        db.session.commit()
    else:
        key_name = ag.extra_attrs['aws']['key-pair']['KeyName']

    # prepare security group
    grp_name = 'kraken-%s' % ag.name
    sec_grp = None
    try:
        sec_grp = ec2.describe_security_groups(GroupNames=[grp_name])
    except Exception:
        log.exception('IGNORED EXCEPTION')
    if not sec_grp:
        rsp = requests.get('https://checkip.amazonaws.com')
        my_ip = rsp.text.strip()

        default_vpc = list(ec2_res.vpcs.filter(Filters=[{'Name': 'isDefault', 'Values': ['true']}]))[0]
        sec_grp = default_vpc.create_security_group(GroupName=grp_name,
                                                    Description='kraken sec group that allows only incomming ssh')
        ip_perms = [{
            # SSH ingress open to only the specified IP address
            'IpProtocol': 'tcp', 'FromPort': 22, 'ToPort': 22,
            'IpRanges': [{'CidrIp': '%s/32' % my_ip}]}]
        sec_grp.authorize_ingress(IpPermissions=ip_perms)
        sec_grp_id = sec_grp.id
    else:
        sec_grp_id = sec_grp['SecurityGroups'][0]['GroupId']

    # get AMI ID
    ami_id = system.name

    # define tags
    tags = [{'ResourceType': 'instance',
             'Tags': [{'Key': 'kraken-group', 'Value': '%d' % ag.id}]}]

    # prepare init script
    init_script = """#!/usr/bin/env bash
                     exec > >(tee /var/log/user-data.log|logger -t user-data -s 2>/dev/console) 2>&1
                     wget -O agent {server_url}/install/agent
                     chmod a+x agent
                     ./agent install -s {server_url} -m {minio_addr} -c {clickhouse_addr}
                  """
    init_script = init_script.format(server_url=server_url, minio_addr=minio_addr, clickhouse_addr=clickhouse_addr)
    if aws.get('init_script', False):
        init_script += aws['init_script']

    # CPU Credits spec
    if aws.get('cpu_credits_unlimited', False):
        cpu_credits = 'unlimited'
    else:
        cpu_credits = 'standard'

    # spot instance
    instance_market_opts = {}
    if aws.get('spot_instance', False):
        instance_market_opts = {
            'MarketType': 'spot',
        }

    # collect all params
    params = dict(ImageId=ami_id,
                  MinCount=num,
                  MaxCount=num,
                  KeyName=key_name,
                  SecurityGroupIds=[sec_grp_id],
                  TagSpecifications=tags,
                  InstanceType=aws['instance_type'],
                  Monitoring={'Enabled': aws.get('monitoring', False)},
                  InstanceMarketOptions=instance_market_opts,
                  UserData=init_script)

    if 't2' in aws['instance_type'] or 't3' in aws['instance_type']:
        params['CreditSpecification'] = {'CpuCredits': cpu_credits}

    # create AWS EC2 instances
    instances = ec2_res.create_instances(**params)

    log.info('spawning new EC2 VMs for agents %s', instances)

    sys_id = system.id if system.executor == 'local' else 0

    now = utils.utcnow()
    for i in instances:
        try:
            i.wait_until_running()
        except Exception:
            log.exception('IGNORED EXCEPTION')
            continue
        i.load()
        name = '.'.join(i.public_dns_name.split('.')[:2])
        address = i.private_ip_address
        a = None
        params = dict(name=name,
                      address=address,
                      ip_address=i.public_ip_address,
                      extra_attrs=dict(system=sys_id, instance_id=i.id),
                      authorized=True,
                      last_seen=now)
        try:
            a = Agent(**params)
            db.session.commit()
        except Exception:
            db.session.rollback()
            a = Agent.query.filter_by(deleted=None, address=address).one_or_none()
            if a:
                for f, val in params.items():
                    setattr(a, f, val)
                db.session.commit()
            else:
                log.info('agent %s duplicated but cannot find it', address)
                raise

        AgentAssignment(agent=a, agents_group=ag)
        db.session.commit()
        log.info('spawned new agent %s on EC2 instance %s', a, i)


def destroy_aws_ec2_vm(access_key, secret_access_key, depl, agent):
    region = depl['region']
    ec2 = boto3.resource('ec2', region_name=region, aws_access_key_id=access_key, aws_secret_access_key=secret_access_key)

    instance_id = agent.extra_attrs['instance_id']
    log.info('terminate ec2 vm %s', instance_id)
    try:
        i = ec2.Instance(instance_id)
        i.terminate()
    except Exception:
        log.exception('IGNORED EXCEPTION')


# AWS ECS FARGATE #############################################################

def create_aws_ecs_fargate_tasks(aws, access_key, secret_access_key,
                                 ag, system, num,
                                 server_url, minio_addr, clickhouse_addr):
    region = aws['region']
    ec2 = boto3.client("ec2", region_name=region, aws_access_key_id=access_key, aws_secret_access_key=secret_access_key)
    ecs = boto3.client('ecs', region_name=region, aws_access_key_id=access_key, aws_secret_access_key=secret_access_key)

    system_norm = system.name.replace(':', '_').replace('/', '_').replace('.', '_')
    task_def_name = 'kraken-agent-1-%s' % system_norm

    # if there is no task definition yet then create one
    response = ecs.list_task_definitions(familyPrefix=task_def_name)
    if len(response['taskDefinitionArns']) == 0:
        response = ecs.register_task_definition(
            family=task_def_name,
            containerDefinitions=[{
                "name": "kraken-agent",
                "image": system.name,
                "logConfiguration": {
                    "logDriver": "awslogs",
                    "options": {
                        "awslogs-group": "/ecs/kraken-agent-definition",
                        "awslogs-region": region,
                        "awslogs-stream-prefix": "ecs"
                    }
                },
            }],
            requiresCompatibilities=['FARGATE'],
            memory="512",
            cpu="256",
            networkMode="awsvpc",
            executionRoleArn="arn:aws:iam::214272040963:role/ecsTaskExecutionRole",
        )

    # prepare command for container
    cmd = 'apt-get update && apt-get install -y --no-install-recommends ca-certificates sudo wget python3'
    cmd += ' && mkdir -p /opt/kraken'
    cmd += ' && wget -O /opt/kraken/kkagent {server_url}/install/agent'
    cmd += ' && wget -O /opt/kraken/kktool {server_url}/install/tool'
    cmd += ' && chmod a+x /opt/kraken/kkagent /opt/kraken/kktool'
    cmd += ' && mkdir -p /tmp/kk-jobs'
    cmd += ' && /opt/kraken/kkagent run -d /tmp/kk-jobs -s {server_url} -m {minio_addr} -c {clickhouse_addr}'
    cmd = cmd.format(server_url=server_url, minio_addr=minio_addr, clickhouse_addr=clickhouse_addr)

    cluster = aws['cluster']
    subnets = aws['subnets'].split(',')
    security_groups = aws['security_groups'].split(',')

    response = ecs.run_task(
        taskDefinition=task_def_name,
        count=num,
        cluster=cluster,
        launchType="FARGATE",
        networkConfiguration={
            'awsvpcConfiguration': {
                'subnets': subnets,
                'securityGroups': security_groups,
                'assignPublicIp': 'ENABLED'
            }
        },
        overrides={
            'containerOverrides': [{
                'name': 'kraken-agent',
                'command': [
                    'bash', '-c', cmd
                ],
                'environment': [
                    {'name': 'KRAKEN_SERVER_ADDR', 'value': server_url},
                    {'name': 'KRAKEN_MINIO_ADDR', 'value': minio_addr},
                    {'name': 'KRAKEN_CLICKHOUSE_ADDR', 'value': clickhouse_addr},
                ],
                #'cpu': 123,
                #'memory': 123,
                #'memoryReservation': 123,
            }],
            #'cpu': 'string',
            #'memory': 'string',
        }
    )

    if response['ResponseMetadata']['HTTPStatusCode'] != 200:
        log.warning('some problem occured')
        log.warning(response)
        return

    all_tasks = [t['taskArn'] for t in response['tasks']]
    tasks_ready = []
    tasks_not_ready = all_tasks[:]
    erred_task = None
    while len(tasks_not_ready) > 0 and not erred_task:
        resp = ecs.describe_tasks(cluster=cluster,
                                  tasks=tasks_not_ready)
        tasks_ready = []
        tasks_not_ready = []
        for task in resp['tasks']:
            if task['lastStatus'] == 'STOPPED':
                erred_task = task
                break
            if task['lastStatus'] != 'RUNNING':
                tasks_not_ready.append(task['taskArn'])
                continue
            eni = None
            for field in task['attachments'][0]['details']:
                if field['name'] == 'networkInterfaceId':
                    eni = field['value']
            if not eni:
                tasks_not_ready.append(task['taskArn'])
                continue
            iface_resp = ec2.describe_network_interfaces(NetworkInterfaceIds=[eni])
            priv_ip = iface_resp['NetworkInterfaces'][0]['PrivateIpAddress']
            pub_dns = iface_resp['NetworkInterfaces'][0]['Association']['PublicDnsName']
            pub_ip = iface_resp['NetworkInterfaces'][0]['Association']['PublicIp']
            name = '.'.join(pub_dns.split('.')[:2])
            tasks_ready.append(dict(task_arn=task['taskArn'],
                                    name=name,
                                    address=priv_ip,
                                    ip_address=pub_ip))
        if tasks_not_ready:
            time.sleep(2)

    if erred_task:
        msg = erred_task['containers'][0]['reason']
        log.warning('problem with starting task %s: %s', erred_task['taskArn'], msg)
        # stop all tasks
        for t in all_tasks:
            try:
                ecs.stop_task(cluster=cluster,
                              task=t,
                              reason='stopping other tasks due to an error')
            except Exception:
                log.exception('IGNORED EXCEPTION')

    sys_id = system.id if system.executor == 'local' else 0

    now = utils.utcnow()
    for task in tasks_ready:
        address = task['address']
        a = None
        params = dict(name=task['name'],
                      address=address,
                      ip_address=task['ip_address'],
                      extra_attrs=dict(system=sys_id, task_arn=task['task_arn']),
                      authorized=True,
                      last_seen=now)
        try:
            a = Agent(**params)
            db.session.commit()
        except Exception:
            db.session.rollback()
            a = Agent.query.filter_by(deleted=None, address=address).one_or_none()
            if a:
                for f, val in params.items():
                    setattr(a, f, val)
                db.session.commit()
            else:
                log.info('agent %s duplicated but cannot find it', address)
                raise

        AgentAssignment(agent=a, agents_group=ag)
        db.session.commit()
        log.info('spawned new agent %s on ECS Fargate task %s', a, task)


def destroy_aws_ecs_task(access_key, secret_access_key, depl, agent):
    region = depl['region']
    ecs = boto3.client('ecs', region_name=region, aws_access_key_id=access_key, aws_secret_access_key=secret_access_key)

    task_arn = agent.extra_attrs['task_arn']
    log.info('terminate ecs task %s', task_arn)
    try:
        ecs.stop_task(cluster=depl['cluster'],
                      task=task_arn,
                      reason='stopping task that completed the job')
    except Exception:
        log.exception('IGNORED EXCEPTION')


# AZURE VM ####################################################################

def check_azure_settings():
    subscription_id = get_setting('cloud', 'azure_subscription_id')
    if not subscription_id:
        return 'Azure Subscription ID is empty'
    # TODO
    # if len(access_key) < 16:
    #     return 'AWS access key is too short'
    # if len(access_key) > 128:
    #     return 'AWS access key is too long'

    tenant_id = get_setting('cloud', 'azure_tenant_id')
    if not tenant_id:
        return 'Azure Tenant ID is empty'

    client_id = get_setting('cloud', 'azure_client_id')
    if not client_id:
        return 'Azure Client ID is empty'

    client_secret = get_setting('cloud', 'azure_client_secret')
    if not client_secret:
        return 'Azure Client Secret is empty'

    try:
        credential = ClientSecretCredential(tenant_id=tenant_id, client_id=client_id, client_secret=client_secret)
        subscription_client = SubscriptionClient(credential)
        subscription_client.subscriptions.list_locations(subscription_id)
    except Exception as ex:
        return str(ex)

    return 'ok'


def _create_azure_vm(group_name, location,
                     server_url, minio_addr, clickhouse_addr,
                     credential, subscription_id):
    instance_id = str(random.randrange(9999999999))

    print(f"Provisioning a virtual machine...some operations might take a minute or two.")

    # Step 1: Provision a resource group

    # Obtain the management object for resources, using the credentials from the CLI login.
    resource_client = ResourceManagementClient(credential, subscription_id)

    # Provision the resource groups.
    global_rg = "kraken-rg"
    rg_result = resource_client.resource_groups.create_or_update(
        global_rg,
        {
            "location": location
        }
    )
    print(f"Provisioned global resource group {rg_result.name} in the {rg_result.location} region")

    group_rg = "kraken-%s-rg" % group_name
    rg_result = resource_client.resource_groups.create_or_update(
        group_rg,
        {
            "location": location
        }
    )
    print(f"Provisioned resource group {rg_result.name} in the {rg_result.location} region")


    # For details on the previous code, see Example: Provision a resource group
    # at https://docs.microsoft.com/azure/developer/python/azure-sdk-example-resource-group


    # Step 2: provision a virtual network

    # A virtual machine requires a network interface client (NIC). A NIC requires
    # a virtual network and subnet along with an IP address. Therefore we must provision
    # these downstream components first, then provision the NIC, after which we
    # can provision the VM.

    # Network and IP address names
    vnet_name = "kraken-vnet"
    subnet_name = "kraken-subnet"

    ip_config_name = "kraken-%s-ip-config" % instance_id
    ip_name = "kraken-%s-ip" % instance_id
    nic_name = "kraken-%s-nic" % instance_id
    vm_name = "kraken-agent-%s-vm" % instance_id

    # Obtain the management object for networks
    network_client = NetworkManagementClient(credential, subscription_id)

    # if VNET exists then use it, otherwise create new one
    try:
        vnet_result = network_client.virtual_networks.get(global_rg, vnet_name)
    except azure.core.exceptions.ResourceNotFoundError:
        # Provision the virtual network and wait for completion
        poller = network_client.virtual_networks.begin_create_or_update(
            global_rg,
            vnet_name,
            {
                "location": location,
                "address_space": {
                    "address_prefixes": ["10.0.0.0/16"]
                }
            })

        vnet_result = poller.result()

    print(f"Provisioned virtual network {vnet_result.name} with address prefixes {vnet_result.address_space.address_prefixes}")

    #########
    security_group_name = 'kraken-nsg'
    try:
        security_group = network_client.network_security_groups.get(global_rg, security_group_name)
    except azure.core.exceptions.ResourceNotFoundError:
        #security_group_params = NetworkSecurityGroup(
        #    id="testnsg",
        #    location=self.location,
        #    tags={"name": security_group_name}
        #)
        poller = network_client.network_security_groups.begin_create_or_update(
            global_rg,
            security_group_name,
            {
                "location": location,
            }
        )
        security_group = poller.result()

    ssh_security_rule_name = 'kraken-ssh-rule'
    try:
        ssh_security_rule = network_client.security_rules.get(global_rg, security_group_name, ssh_security_rule_name)
    except azure.core.exceptions.ResourceNotFoundError:
        poller = network_client.security_rules.begin_create_or_update(
            global_rg,
            security_group_name,
            ssh_security_rule_name,
            {
                'access': SecurityRuleAccess.allow,
                'description': 'SSH security rule',
                'destination_address_prefix': '*',
                'destination_port_range': '22',
                'direction': SecurityRuleDirection.inbound,
                'priority': 400,
                'protocol': SecurityRuleProtocol.tcp,
                'source_address_prefix': '*',
                'source_port_range': '*',
            }
        )
        ssh_security_rule = poller.result()
    #########

    # Step 3: Provision the subnet and wait for completion
    # if subnet exists then use it, otherwise create new one
    try:
        subnet_result = network_client.subnets.get(global_rg, vnet_name, subnet_name)
    except Exception:
        poller = network_client.subnets.begin_create_or_update(
            global_rg,
            vnet_name,
            subnet_name,
            {
                "address_prefix": "10.0.0.0/24",
            }
        )
        subnet_result = poller.result()

    print(f"Provisioned virtual subnet {subnet_result.name} with address prefix {subnet_result.address_prefix}")

    # Step 4: Provision an IP address and wait for completion
    poller = network_client.public_ip_addresses.begin_create_or_update(
        group_rg,
        ip_name,
        {
            "location": location,
            "sku": { "name": "Standard" },
            "public_ip_allocation_method": "Static",
            "public_ip_address_version" : "IPV4"
        }
    )
    ip_address_result = poller.result()

    print(f"Provisioned public IP address {ip_address_result.name} with address {ip_address_result.ip_address}")

    # Step 5: Provision the network interface client
    poller = network_client.network_interfaces.begin_create_or_update(
        group_rg,
        nic_name,
        {
            "location": location,
            "ip_configurations": [{
                "name": ip_config_name,
                "subnet": { "id": subnet_result.id },
                "public_ip_address": {"id": ip_address_result.id }
            }],
            "network_security_group": {"id": security_group.id }
        }
    )

    nic_result = poller.result()

    print(f"Provisioned network interface client {nic_result.name}")

    # Step 6: Provision the virtual machine

    # Obtain the management object for virtual machines
    compute_client = ComputeManagementClient(credential, subscription_id)

    USERNAME = "kraken"
    PASSWORD = "kraken123!"

    print(f"Provisioning virtual machine {vm_name}; this operation might take a few minutes.")

    # Provision the VM specifying only minimal arguments, which defaults to an Ubuntu 18.04 VM
    # on a Standard DS1 v2 plan with a public IP address and a default virtual network/subnet.

    init_script = """#!/usr/bin/env bash
                     exec > >(tee /var/log/user-data.log|logger -t user-data -s 2>/dev/console) 2>&1
                     wget -O agent {server_url}/install/agent
                     chmod a+x agent
                     ./agent install -s {server_url} -m {minio_addr} -c {clickhouse_addr}
                  """
    init_script = init_script.format(server_url=server_url, minio_addr=minio_addr, clickhouse_addr=clickhouse_addr)

    poller = compute_client.virtual_machines.begin_create_or_update(
        group_rg,
        vm_name,
        {
            "location": location,
            "storage_profile": {
                "image_reference": {
                    "publisher": 'Canonical',
                    "offer": "0001-com-ubuntu-server-focal",
                    "sku": "20_04-lts",
                    "version": "latest"
                }
            },
            "hardware_profile": {
                "vm_size": "Standard_B1ls"
            },
            "os_profile": {
                "computer_name": vm_name,
                "admin_username": USERNAME,
                "admin_password": PASSWORD,
            },
            "network_profile": {
                "network_interfaces": [{
                    "id": nic_result.id,
                }]
            },
            "user_data": base64.b64encode(init_script.encode('utf-8')).decode()
        }
    )

    vm_result = poller.result()

    print(f"Provisioned virtual machine {vm_result.name}")

    # VM params
    address = nic_result.ip_configurations[0].private_ip_address
    params = dict(name=vm_name,
                  address=address,
                  ip_address=ip_address_result.ip_address,
                  extra_attrs=dict(
                      system='sys_id',
                      instance_id=instance_id),
                  authorized=True,
                  last_seen='now')
    print(params)
    return params


def create_azure_vms(depl, access_key, secret_access_key,
                     ag, system, num,
                     server_url, minio_addr, clickhouse_addr):

    # Acquire a credential object using service principal authentication.
    subscription_id = 'a80e8684-5866-41ef-ac39-e4058f7a490f'
    tenant_id = '698e4063-ab0b-469b-b755-0204bc97db4b'
    client_id = 'a756f034-5c2b-4cc1-98a7-a24bff5238bb'
    client_secret = '2xEtx7CHJ.jloWrX5p7p5lL-IW._y.t07U'
    credential = ClientSecretCredential(tenant_id=tenant_id, client_id=client_id, client_secret=client_secret)

    _create_azure_vm(group_name, location,
                     server_url, minio_addr, clickhouse_addr,
                     credential, subscription_id)


def delete_azure_vm(group_name, vm_name, instance_id):
    log.info('deleting azure vm: %s', vm_name)

    rg = 'kraken-%s-rg' % group_name

    # Acquire a credential object using service principal authentication.
    subscription_id = 'a80e8684-5866-41ef-ac39-e4058f7a490f'
    tenant_id = '698e4063-ab0b-469b-b755-0204bc97db4b'
    client_id = 'a756f034-5c2b-4cc1-98a7-a24bff5238bb'
    client_secret = '2xEtx7CHJ.jloWrX5p7p5lL-IW._y.t07U'
    credential = ClientSecretCredential(tenant_id=tenant_id, client_id=client_id, client_secret=client_secret)

    cc = ComputeManagementClient(credential, subscription_id)
    vm = cc.virtual_machines.get(rg, vm_name)
    disk_name = vm.storage_profile.os_disk.name
    cc.virtual_machines.begin_delete(rg, vm_name).wait()
    cc.disks.begin_delete(rg, disk_name)

    ip_config_name = "kraken-%s-ip-config" % instance_id
    ip_name = "kraken-%s-ip" % instance_id
    nic_name = "kraken-%s-nic" % instance_id
    nc = NetworkManagementClient(credential, subscription_id)
    nc.network_interfaces.begin_delete(rg, nic_name).wait()
    nc.public_ip_addresses.begin_delete(rg, ip_name).wait()

    log.info('deleted azure vm: %s', vm_name)


####################################################################


def create_machines(depl, access_key, secret_access_key,
                   ag, system, num,
                   server_url, minio_addr, clickhouse_addr):
    method = ag.deployment['method']
    if method == consts.AGENT_DEPLOYMENT_METHOD_AWS_EC2:
        create_ec2_vms(depl, access_key, secret_access_key,
                       ag, system, num,
                       server_url, minio_addr, clickhouse_addr)

    elif method == consts.AGENT_DEPLOYMENT_METHOD_AWS_ECS_FARGATE:
        create_aws_ecs_fargate_tasks(depl, access_key, secret_access_key,
                                     ag, system, num,
                                     server_url, minio_addr, clickhouse_addr)

    elif method == consts.AGENT_DEPLOYMENT_METHOD_AZURE_VM:
        create_azure_vms(depl, access_key, secret_access_key,
                         ag, system, num,
                         server_url, minio_addr, clickhouse_addr)

    else:
        raise NotImplementedError('deployment method %s not supported' % str(ag.deployment['method']))


def destroy_machine(access_key, secret_access_key, method, depl, agent):
    if method == consts.AGENT_DEPLOYMENT_METHOD_AWS_EC2:
        destroy_aws_ec2_vm(access_key, secret_access_key, depl, agent)
    elif method == consts.AGENT_DEPLOYMENT_METHOD_AWS_ECS_FARGATE:
        destroy_aws_ecs_task(access_key, secret_access_key, depl, agent)


#if __name__ == '__main__':
#    sys_img = 'ubuntu:20.04'
#    #sys_img = 'public.ecr.aws/kraken-ci/kkagent:0.627'
#    main(sys_img, 'http://89.65.138.0:8080', '89.65.138.0:6363', '89.65.138.0:9999', '89.65.138.0:9001')
