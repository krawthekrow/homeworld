import contextlib
import os
import subprocess
import sys
import time

import authority
import command
import configuration
import keycrypt
import resource
import ssh


def escape_shell(param: str) -> str:
    # replaces ' -> '"'"'
    return "'" + param.replace("'", "'\"'\"'") + "'"


def ssh_raw(ops, name: str, node: configuration.Node, script: str, in_directory: str=None, redirect_to: str=None)\
        -> None:
    if redirect_to:
        script = "(%s) >%s" % (script, escape_shell(redirect_to))
    if in_directory:
        script = "cd %s && %s" % (escape_shell(in_directory), script)
    ops.add_operation(name.replace('@HOST', node.hostname),
                      lambda: ssh.check_ssh(node, script))

def ssh_cmd(ops, name: str, node: configuration.Node, *argv: str, in_directory: str=None, redirect_to: str=None)\
        -> None:
    ssh_raw(ops, name, node, " ".join(escape_shell(param) for param in argv),
            in_directory=in_directory, redirect_to=redirect_to)

def ssh_mkdir(ops, name: str, node: configuration.Node, *paths: str, with_parents: bool=True) -> None:
    options = ["-p"] if with_parents else []
    ssh_cmd(ops, name, node, "mkdir", *(options + ["--"] + list(paths)))

def ssh_upload_path(ops, name: str, node: configuration.Node, source_path: str, dest_path: str) -> None:
    ops.add_operation(name.replace('@HOST', node.hostname),
                      lambda: ssh.check_scp_up(node, source_path, dest_path))

def ssh_upload_bytes(ops, name: str, node: configuration.Node, source_bytes: bytes, dest_path: str) -> None:
    ops.add_operation(name.replace('@HOST', node.hostname),
                      lambda: ssh.upload_bytes(node, source_bytes, dest_path))


AUTHORITY_DIR = "/etc/homeworld/keyserver/authorities"
STATICS_DIR = "/etc/homeworld/keyserver/static"
CONFIG_DIR = "/etc/homeworld/config"
KEYCLIENT_DIR = "/etc/homeworld/keyclient"
KEYTAB_PATH = "/etc/krb5.keytab"


def setup_keyserver(ops: Operations) -> None:
    config = configuration.get_config()
    for node in config.nodes:
        if node.kind != "supervisor":
            continue
        ops.ssh_mkdir("create directories on @HOST", node, AUTHORITY_DIR, STATICS_DIR, CONFIG_DIR)
        for name, data in authority.iterate_keys_decrypted():
            # TODO: keep these keys in memory
            if "/" in name:
                command.fail("found key in upload list with invalid filename")
            # TODO: avoid keeping these keys in memory for this long
            ops.ssh_upload_bytes("upload authority %s to @HOST" % name, node, data, os.path.join(AUTHORITY_DIR, name))
        ops.ssh_upload_bytes("upload cluster config to @HOST", node,
                             configuration.get_cluster_conf().encode(), STATICS_DIR + "/cluster.conf")
        ops.ssh_upload_path("upload cluster setup to @HOST", node,
                            configuration.Config.get_setup_path(), CONFIG_DIR + "/setup.yaml")
        ops.ssh("enable keyserver on @HOST", node, "systemctl", "enable", "keyserver.service")
        ops.ssh("start keyserver on @HOST", node, "systemctl", "restart", "keyserver.service")


def admit_keyserver(ops: Operations) -> None:
    config = configuration.get_config()
    for node in config.nodes:
        if node.kind != "supervisor":
            continue
        domain = node.hostname + "." + config.external_domain
        ops.ssh("request bootstrap token for @HOST", node,
                "keyinitadmit", domain,
                redirect_to=KEYCLIENT_DIR + "/bootstrap.token")
        # TODO: do we need to poke the keyclient to make sure it tries again?
        # TODO: don't wait four seconds if it isn't necessary
        ops.ssh("kick keyclient daemon on @HOST", node, "systemctl", "restart", "keyclient")
        # if it doesn't exist, this command will fail.
        ops.ssh("confirm that @HOST was admitted", node, "test", "-e", KEYCLIENT_DIR + "/granting.pem")
        ops.ssh("enable auth-monitor daemon on @HOST", node, "systemctl", "enable", "auth-monitor")
        ops.ssh("start auth-monitor daemon on @HOST", node, "systemctl", "restart", "auth-monitor")


def modify_keygateway(ops: Operations, overwrite_keytab: bool) -> None:
    config = configuration.get_config()
    if not config.is_kerberos_enabled():
        print("keygateway disabled; skipping")
        return
    for node in config.nodes:
        if node.kind != "supervisor":
            continue
        # keytab is stored encrypted in the configuration folder
        keytab = os.path.join(configuration.get_project(), "keytab.%s.crypt" % node.hostname)
        decrypted = keycrypt.gpg_decrypt_to_memory(keytab)
        def safe_upload_keytab(node=node):
            if not overwrite_keytab:
                try:
                    existing_keytab = ssh.check_ssh_output(node, "cat", KEYTAB_PATH)
                except subprocess.CalledProcessError as e_test:
                    # if there is no existing keytab, cat will fail with error code 1
                    if e_test.returncode != 1:
                        command.fail(e_test)
                    print("no existing keytab found, uploading local keytab")
                else:
                    if existing_keytab != decrypted:
                        command.fail("existing keytab does not match local keytab")
                    return # existing keytab matches local keytab, no action required
            ssh.upload_bytes(node, decrypted, KEYTAB_PATH)
        ops.add_operation("upload keytab for @HOST", safe_upload_keytab, node)
        ops.ssh("enable keygateway on @HOST", node, "systemctl", "enable", "keygateway")
        ops.ssh("restart keygateway on @HOST", node, "systemctl", "restart", "keygateway")


def setup_keygateway(ops: Operations) -> None:
    modify_keygateway(ops, False)


def update_keygateway(ops: Operations) -> None:
    modify_keygateway(ops, True)


def setup_supervisor_ssh(ops: Operations) -> None:
    config = configuration.get_config()
    for node in config.nodes:
        if node.kind != "supervisor":
            continue
        ssh_config = resource.get_resource("sshd_config")
        ops.ssh_upload_bytes("upload new ssh configuration to @HOST", node, ssh_config, "/etc/ssh/sshd_config")
        ops.ssh("reload ssh configuration on @HOST", node, "systemctl", "restart", "ssh")
        ops.ssh_raw("shift aside old authorized_keys on @HOST", node,
                "if [ -f /root/.ssh/authorized_keys ]; then " +
                "mv /root/.ssh/authorized_keys " +
                "/root/original_authorized_keys; fi")


def modify_dns_bootstrap(ops: Operations, is_install: bool) -> None:
    config = configuration.get_config()
    for node in config.nodes:
        strip_cmd = "grep -vF AUTO-HOMEWORLD-BOOTSTRAP /etc/hosts >/etc/hosts.new && mv /etc/hosts.new /etc/hosts"
        ops.ssh_raw("strip bootstrapped dns on @HOST", node, strip_cmd)
        if is_install:
            for hostname, ip in config.dns_bootstrap.items():
                new_hosts_line = "%s\t%s # AUTO-HOMEWORLD-BOOTSTRAP" % (ip, hostname)
                strip_cmd = "echo %s >>/etc/hosts" % escape_shell(new_hosts_line)
                ops.ssh_raw("bootstrap dns on @HOST: %s" % hostname, node, strip_cmd)


def modify_temporary_dns(node: configuration.Node, additional: dict) -> None:
    ssh.check_ssh(node, "grep -vF AUTO-TEMP-DNS /etc/hosts >/etc/hosts.new && mv /etc/hosts.new /etc/hosts")
    for hostname, ip in additional.items():
        new_hosts_line = "%s\t%s # AUTO-TEMP-DNS" % (ip, hostname)
        ssh.check_ssh(node, "echo %s >>/etc/hosts" % escape_shell(new_hosts_line))


def dns_bootstrap_lines() -> str:
    config = configuration.get_config()
    dns_hosts = config.dns_bootstrap.copy()
    dns_hosts["homeworld.private"] = config.keyserver.ip
    for node in config.nodes:
        full_hostname = "%s.%s" % (node.hostname, config.external_domain)
        if node.hostname in dns_hosts:
            command.fail("redundant /etc/hosts entry: %s", node.hostname)
        if full_hostname in dns_hosts:
            command.fail("redundant /etc/hosts entry: %s", full_hostname)
        dns_hosts[node.hostname] = node.ip
        dns_hosts[full_hostname] = node.ip
    return "".join("%s\t%s # AUTO-HOMEWORLD-BOOTSTRAP\n" % (ip, hostname) for hostname, ip in dns_hosts.items())


def setup_dns_bootstrap(ops: Operations) -> None:
    modify_dns_bootstrap(ops, True)


def teardown_dns_bootstrap(ops: Operations) -> None:
    modify_dns_bootstrap(ops, False)


def setup_bootstrap_registry(ops: Operations) -> None:
    config = configuration.get_config()
    for node in config.nodes:
        if node.kind != "supervisor":
            continue

        ops.ssh("enable docker-registry on @HOST", node, "systemctl", "enable", "docker-registry")
        ops.ssh("restart docker-registry on @HOST", node, "systemctl", "restart", "docker-registry")

        ops.ssh("unmask nginx on @HOST", node, "systemctl", "unmask", "nginx")
        ops.ssh("enable nginx on @HOST", node, "systemctl", "enable", "nginx")
        ops.ssh("restart nginx on @HOST", node, "systemctl", "restart", "nginx")


def update_registry(ops: Operations) -> None:
    config = configuration.get_config()
    for node in config.nodes:
        if node.kind != "supervisor":
            continue

        ops.ssh("update apt repositories on @HOST", node, "apt-get", "update")
        ops.ssh("update package of OCIs on @HOST", node, "apt-get", "install", "-y", "homeworld-oci-pack")
        ops.ssh("upgrade apt packages on @HOST", node, "apt-get", "upgrade", "-y")
        ops.ssh("re-push OCIs to registry on @HOST", node, "/usr/lib/homeworld/push-ocis.sh")


def setup_prometheus(ops: Operations) -> None:
    config = configuration.get_config()
    for node in config.nodes:
        if node.kind != "supervisor":
            continue
        ops.ssh_upload_bytes("upload prometheus config to @HOST", node, configuration.get_prometheus_yaml().encode(),
                             "/etc/prometheus.yaml")
        ops.ssh("enable prometheus on @HOST", node, "systemctl", "enable", "prometheus")
        ops.ssh("restart prometheus on @HOST", node, "systemctl", "restart", "prometheus")


def wrapop(desc: str, f):
    def wrap_param_tx(opts):
        ops = Operations()
        return {'ops': ops, **opts}, ops.run_operations
    return command.wrap(desc, f, wrap_param_tx)


main_command = command.mux_map("commands about setting up a cluster", {
    "keyserver": wrapop("deploy keys and configuration for keyserver; start keyserver", setup_keyserver),
    "self-admit": wrapop("admit the keyserver into the cluster during bootstrapping", admit_keyserver),
    "keygateway": wrapop("deploy keytab and start keygateway", setup_keygateway),
    "update-keygateway": wrapop("update keytab and restart keygateway", update_keygateway),
    "supervisor-ssh": wrapop("configure supervisor SSH access", setup_supervisor_ssh),
    "dns-bootstrap": wrapop("switch cluster nodes into 'bootstrapped DNS' mode", setup_dns_bootstrap),
    "stop-dns-bootstrap": wrapop("switch cluster nodes out of 'bootstrapped DNS' mode", teardown_dns_bootstrap),
    "bootstrap-registry": wrapop("bring up the bootstrap container registry on the supervisor nodes", setup_bootstrap_registry),
    "update-registry": wrapop("upload the latest container versions to the bootstrap container registry", update_registry),
    "prometheus": wrapop("bring up the supervisor node prometheus instance", setup_prometheus),
})
