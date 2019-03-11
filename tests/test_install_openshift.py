import logging
import os
import textwrap
import time
from ceph.parallel import parallel

log = logging.getLogger(__name__)
hostname_map = {}


def run(**kw):
    cluster = kw.get('ceph_cluster')
    try:
        generate_hostname_map(cluster)
        with parallel() as p:
            for node in cluster.get_nodes():
                p.spawn(initial_setup, node)

        # ensure all nodes are up
        for node in cluster.get_nodes():
            if not node_is_alive(node):
                raise RebootError("Node {node.hostname} has not come up after reboot")

        install_openshift(cluster)
        status = verify_cluster_health()
        return status
    except Exception as e:
        log.exception(e)
        return 1


def initial_setup(node):
    """
    Perform initial setup on nodes in cluster

    Args:
        node: Node to perform initial setup on

    Returns: None

    """
    # edit rhsm file
    node.exec_command(sudo=True, cmd="sed -i -e 's,^hostname *=.*,hostname = subscription.rhn.stage.redhat.com,"
                                     ";s,baseurl *=.*,baseurl= https://cdn.redhat.com,' /etc/rhsm/rhsm.conf")

    # setup subscription manager and repos
    node.exec_command(sudo=True, cmd='subscription-manager --force register '
                                     '--serverurl=subscription.rhsm.stage.redhat.com:443/subscription '
                                     '--baseurl=https://cdn.redhat.com --username=qa@redhat.com '
                                     '--password=redhatqa --auto-attach')
    node.exec_command(sudo=True, cmd='subscription-manager refresh')
    node.exec_command(sudo=True, cmd='subscription-manager attach --pool=8a85f98960dbf6510160df23e3367451')
    node.exec_command(sudo=True, cmd='subscription-manager repos --disable="*"')
    node.exec_command(sudo=True, cmd='subscription-manager repos --enable="rhel-7-server-rpms" '
                                     '--enable="rhel-7-server-extras-rpms" --enable="rhel-7-server-ose-3.11-rpms" '
                                     '--enable="rhel-7-server-ansible-2.6-rpms" --enable="rhel-7-fast-datapath-rpms"')

    # install dependencies
    node.exec_command(sudo=True, cmd='yum -y install ansible')
    node.exec_command(sudo=True, cmd='yum -y install vim screen tree wget git net-tools bind-utils iptables-services '
                                     'bridge-utils bash-completion kexec-tools sos psacct')

    # set hostnames
    node.exec_command(sudo=True, cmd=f'hostnamectl set-hostname {hostname_map[node.hostname]}')

    # verify hostnames
    out, err = node.exec_command(cmd='hostname')
    name = out.read().decode().strip()
    out, err = node.exec_command(cmd='hostname -f')
    fname = out.read().decode().strip()
    if name != hostname_map[node.hostname] or fname != hostname_map[node.hostname]:
        raise IncorrectHostnameError(f"`hostname` and/or `hostname -f` do not match generated hostname for "
                                     f"{node.hostname}")

    # setup docker
    # setup_docker(node)

    # yum update
    node.exec_command(sudo=True, cmd='yum -y update')

    # reboot nodes
    node.exec_command(sudo=True, cmd='systemctl reboot', check_ec=False)


def install_openshift(cluster):
    """
    Final setup and execution of openshift-ansible playbooks.
    Essentially only tasks that take place on the master/installer node.
    Args:
        cluster: Cluster to install openshift on

    Returns: None

    """
    master = cluster.get_nodes('master')[0]

    # generate ssh key
    key_path = "/home/cephuser/.ssh/id_rsa"
    master.exec_command(cmd=f"ssh-keygen -b 2048 -f {key_path} -t rsa -q -N ''")

    # copy ssh key to all nodes
    out, _ = master.exec_command(cmd=f"cat {key_path}.pub")
    id_rsa_pub = out.read().decode()

    for node in cluster.get_nodes():
        keys_file = node.write_file(
            file_name='.ssh/authorized_keys', file_mode='a')
        keys_file.write(id_rsa_pub)
        keys_file.flush()

    # install openshift-ansible
    master.exec_command(sudo=True, cmd='yum -y install openshift-ansible')

    # create hosts file
    ansible_dir = "/usr/share/ansible/openshift-ansible"
    write_hosts_file(cluster, ansible_dir)

    # prerequisites
    master.exec_command(cmd=f'cd {ansible_dir} && ansible-playbook -i hosts playbooks/prerequisites.yml -vvv',
                        long_running=True)

    # deploy_cluster
    master.exec_command(cmd=f'cd {ansible_dir} && ansible-playbook -i hosts playbooks/deploy_cluster.yml -vvv',
                        long_running=True)


def verify_cluster_health():
    """
    Verify that the openshift cluster is running

    Returns: 0 if cluster is healthy, 1 if not.

    """
    # todo: determine how to check health of ocs
    healthy = True
    if healthy:
        return 0
    else:
        return 1


def generate_hostname_map(cluster):
    """
    Generate a mapping of host.hostname -> resolvable domain name
    We do this since the resolvable hostnames are different from the instance names
     or hostnames we set initially in openstack

    Args:
        cluster: cluster to create map for

    Returns: None

    """
    global hostname_map
    for node in cluster.get_nodes():
        dashed_ip = "-".join(node.ip_address.split('.'))
        hostname = f'ci-vm-{dashed_ip}.hosted.upshift.rdu2.redhat.com'
        log.info(f'constructed hostname for node {node.hostname}: {hostname}')
        hostname_map.update({node.hostname: hostname})


def node_is_alive(node, attempts=10):
    """
    Ping a node until it is alive.

    Args:
        node: Node to ping
        attempts: Number of times to ping before failing

    Returns: Boolean if node was able to be pinged or not

    """
    while attempts:
        if os.system(f'ping -c 1 {node.ip_address}') == 0:
            return True
        else:
            attempts -= 1
            time.sleep(15)
    return False


def write_hosts_file(cluster, ansible_dir):
    """
    Write the hosts file for openshift installation
    Args:
        cluster: Cluster to install openshift on
        ansible_dir: Directory where we place the hosts file

    Returns: None

    """
    # todo: address hard coded indices for node hostnames
    masters = [hostname_map[master.hostname] for master in cluster.get_nodes('master')]
    computes = [hostname_map[compute.hostname] for compute in cluster.get_nodes('compute')]
    infras = [hostname_map[compute.hostname] for compute in cluster.get_nodes('infra')]

    content = textwrap.dedent(
        f"""
        [OSEv3:children]
        masters
        nodes
        etcd
        
        [OSEv3:vars]
        install_method=rpm
        os_update=false
        install_update_docker=true
        docker_storage_driver=devicemapper
        ansible_ssh_user=cephuser
        ansible_become=true
        openshift_release=v3.11
        oreg_url=registry.access.redhat.com/openshift3/ose-${{component}}:v3.11
        openshift_cockpit_deployer_image=registry.access.redhat.com/openshift3/
        openshift_docker_insecure_registries=registry.access.redhat.com
        openshift_deployment_type=openshift-enterprise
        openshift_web_console_install=true
        openshift_enable_service_catalog=false
        osm_use_cockpit=false
        osm_cockpit_plugins=['cockpit-kubernetes']
        debug_level=5
        openshift_set_hostname=true
        openshift_override_hostname_check=true
        openshift_disable_check=docker_image_availability
        openshift_check_min_host_disk_gb=2
        openshift_check_min_host_memory_gb=1
        openshift_portal_net=172.31.0.0/16
        openshift_master_cluster_method=native
        openshift_clock_enabled=true
        openshift_use_openshift_sdn=true 
        openshift_master_dynamic_provisioning_enabled=true
        openshift_master_cluster_hostname={masters[0]}
        openshift_master_cluster_public_hostname={masters[0]}
        
        [masters]
        {masters[0]}
            
        [etcd]
        {masters[0]}
           
        [nodes]
        {masters[0]} openshift_node_group_name='node-config-master'
        {computes[0]} openshift_node_group_name='node-config-compute'
        {computes[1]} openshift_node_group_name='node-config-compute'
        {infras[0]} openshift_node_group_name='node-config-infra'
        {infras[1]} openshift_node_group_name='node-config-infra'
        """)

    log.info(content)
    master = cluster.get_nodes('master')[0]
    hosts_file = master.write_file(sudo=True, file_name=f'{ansible_dir}/hosts', file_mode='w')
    hosts_file.write(content)
    hosts_file.flush()


def setup_docker(node):
    # install and enable docker
    node.exec_command(sudo=True, cmd='yum -y install docker-1.13.1')
    node.exec_command(sudo=True, cmd='systemctl enable docker')
    node.exec_command(sudo=True, cmd='systemctl start docker')

    # pull docker images
    registries_conf = node.write_file(sudo=True, file_name='/etc/containers/registries.conf', file_mode='a')
    registries_conf.write('brew-pulp-docker01.web.prod.ext.phx2.redhat.com:8888')
    registries_conf.flush()
    images = ["rhel7/etcd:3.2.22", "openshift3/ose-node:v3.10", "openshift3/ose-haproxy-router:v3.10",
              "openshift3/ose-deployer:v3.10", "openshift3/ose-control-plane:v3.10", "openshift3/ose-web-console:v3.10",
              "openshift3/ose-docker-registry:v3.10", "openshift3/ose-pod:v3.10", "openshift3/oauth-proxy:v3.10",
              "openshift3/registry-console:v3.10", "openshift3/jenkins-2-rhel7", "rhscl/mongodb-32-rhel7"]
    for image in images:
        node.exec_command(sudo=True, cmd=f'docker pull {image}')


class RebootError(Exception):
    pass


class IncorrectHostnameError(Exception):
    pass

