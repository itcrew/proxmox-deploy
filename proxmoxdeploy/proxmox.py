# proxmox-deploy is cli-based deployment tool for Proxmox
#
# Copyright (c) 2015 Nick Douma <n.douma@nekoconeko.nl>
#
# This program is free software: you can redistribute it and/or modify it under
# the terms of the GNU General Public License as published by the Free Software
# Foundation, either version 3 of the License, or (at your option) any later
# version.
#
# This program is distributed in the hope that it will be useful, but WITHOUT
# ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or
# FITNESS FOR A PARTICULAR PURPOSE. See the GNU General Public License for more
# details.
#
# You should have received a copy of the GNU General Public License along with
# this program. If not, see http://www.gnu.org/licenses/.

from .exceptions import SSHCommandInvocationException
from .questions import QuestionGroup, IntegerQuestion, EnumQuestion, NoAskQuestion
import math
import os.path


def ask_proxmox_questions(proxmox):
    """
    Asks the user questions about the Proxmox VM to provision.

    Parameters
    ----------
    proxmox: ProxmoxClient

    Returns
    -------
    dict of key-value pairs of answered questions.
    """
    node_q = EnumQuestion("Proxmox Node to create VM on",
                          valid_answers=proxmox.get_nodes())
    node_q.ask()
    chosen_node = node_q.answer

    storage_q = EnumQuestion("Storage to create disk on",
                             valid_answers=proxmox.get_storage(chosen_node))
    storage_q.ask()
    chosen_storage = storage_q.answer

    proxmox_questions = QuestionGroup([
        ("node", NoAskQuestion(question=None, default=chosen_node)),
        ("storage", NoAskQuestion(question=None, default=chosen_storage)),
        ("cpu", IntegerQuestion(
            "Amount of CPUs", min_value=1,
            max_value=proxmox.get_max_cpu(chosen_node))),
        ("memory", IntegerQuestion(
            "Amount of Memory (MB)", min_value=32,
            max_value=proxmox.get_max_memory(chosen_node))),
        ("disk", IntegerQuestion(
            "Size of disk (GB)", min_value=4,
            max_value=proxmox.get_max_disk_size(chosen_node, chosen_storage)))
    ])

    proxmox_questions.ask_all()
    return proxmox_questions.flatten_answers()


class ProxmoxClient(object):
    """
    Wrapper around Proxmoxer, to encapsulate retrieval logic in one place.
    """
    def __init__(self, client):
        """
        Parameters
        ----------
        client: ProxmoxAPI
            ProxmoxAPI intance
        """
        self.client = client

    def get_nodes(self):
        """
        Retrieve a list of available nodes.

        Returns
        -------
        List of node names.
        """
        return [_node['node'] for _node in self.client.nodes.get()]

    def get_max_cpu(self, node=None):
        """
        Get maximum available cpus.

        Parameters
        ----------
        node: str
            If provided, will retrieve the cpu limit for this specific node. If
            not, will return the lowest common denomitor for all nodes.

        Returns
        -------
        Amount of cpus available.
        """
        if node:
            status = self.client.nodes(node).status.get()
            return status['cpuinfo']['cpus'] * status['cpuinfo']['sockets']
        else:
            return min([_node['maxcpu'] for _node in self.client.nodes.get()])

    def get_max_memory(self, node=None):
        """
        Get maximum amount of memory available.

        Parameters
        ----------
        node: str
            If provided, will retrieve the memory limit for this specific node.
            If not, will return the lowest common denomitor for all nodes.

        Returns
        -------
        Amount of memory available in megabytes.
        """
        if node:
            status = self.client.nodes(node).status.get()
            return int(math.floor(
                status['memory']['total'] / 1024 ** 2))
        else:
            return min(
                [int(math.floor(_node['maxmem'] / 1024 ** 2))
                 for _node in self.client.nodes.get()]
            )

    def get_storage(self, node):
        """
        Get available storages.

        Parameters
        ----------
        node: str
            If provided, will retrieve the storages for this specific node. If
            not, will retrieve the global storages.

        Returns
        -------
        List of storages available.
        """
        storages = []
        for storage in self.client.nodes(node).storage.get():
            if "images" in storage['content'].split(","):
                storages.append(storage['storage'])
        return storages

    def get_max_disk_size(self, node=None, storage=None):
        """
        Get the maximum amount of disk space available.

        Parameters
        ----------
        node: str
            If provided, will retrieve the disk limit for this specific node. A
            storage must also be specified. If not, will return the lowest
            common denomitor for all storages.
        storage: str
            Name of storage to lookup maximum amount for.

        Returns
        -------
        Amount of disk space available in gigabytes.
        """
        if node:
            if not storage:
                raise ValueError(
                    "A storage must also be specified for the given node")
            _storage = self.client.nodes(node).storage.get(storage=storage)[0]
            return int(math.floor(
                _storage['avail'] / 1024 ** 3))
        else:
            return min([int(math.floor(_node['maxdisk'] / 1024 ** 3))
                        for _node in self.client.nodes.get()])

    def create_vm(self, node, vmid, name, cpu, memory):
        """
        Creates a VM.

        Parameters
        ----------
        node: str
            Name of the node to create the VM on.
        vmid: int
            ID of the VM.
        name: str
            Name of the VM.
        cpu: int
            Number of CPU cores.
        memory: int
            Megabytes of memory.
        """
        node = self.client.nodes(node)
        node.qemu.create(
            vmid=vmid, name=name, sockets=1, cores=cpu, memory=memory,
            net0="virtio,bridge=vmbr0"
        )

    def _upload_to_storage(self, ssh_session, storage, vmid, filename,
                           diskname, storagename, disk_format="raw",
                           disk_size=None):
        """
        Upload a file into a datastore. The steps executed are:
          1. The file is uploaded via SFTP to /tmp.
          2. A new disk is allocated using `pvesm`.
          3. The path of this disk is retrieved using `pvesm`.
          4. The file is converted and transfered into the disk
          using `qemu-img`.
          5. The temporary file is removed.

        Parameters
        ----------
        ssh_session: ProxmoxBaseSSHSession subclass
            This is an internal class used by proxmoxer, we're using its ssh
            transfer methods for various operations.
        storage: str
            Name of storage to upload the file into.
        vmid: int
            ID of the VM to associate the file with. This is enforced by
            Proxmox.
        filename: str
            Local filename of the file.
        diskname: str
            Name of the disk to allocate.
        storagename: str
            Full canonical name of the disk.
        disk_format: raw or qcow2
            Format of the file. The source type doesn't matter, as we will call
            `qemu-img` to both transfer and convert the file into the disk.
        disk_size: int
            Override the disk size. If not specified, the size is calculated
            from the file. In kilobytes.
        """
        tmpfile = os.path.join("/tmp", os.path.basename(filename))
        with open(filename) as _file:
            ssh_session.upload_file_obj(_file, tmpfile)

        if not disk_size:
            disk_size = int(math.ceil(os.stat(filename).st_size / 1024))

        stdout, stderr = ssh_session._exec(
            "pvesm alloc '{0}' {1} '{2}' {3} -format {4}".format(
                storage, vmid, diskname,
                disk_size, disk_format
            )
        )
        if storagename not in stdout and len(stderr) > 0:
            print stdout
            print stderr
            raise SSHCommandInvocationException(
                "Failed to allocate disk", stdout=stdout, stderr=stderr)

        stdout, stderr = ssh_session._exec(
            "pvesm path '{0}'".format(storagename)
        )

        if len(stderr) > 0:
            raise SSHCommandInvocationException(
                "Failed to get path for disk", stdout=stdout, stderr=stderr)

        devicepath = stdout

        stdout, stderr = ssh_session._exec(
            "qemu-img convert -O {0} '{1}' {2}".format(disk_format, tmpfile,
                                                       devicepath)
        )

        if len(stderr) > 0:
            print stdout
            print stderr
            raise SSHCommandInvocationException(
                "Failed to copy file into disk", stdout=stdout, stderr=stderr)

        ssh_session._exec("rm '{0}'".format(tmpfile))

    def _upload_to_flat_storage(self, storage, vmid, filename, disk_format,
                                disk_label, disk_size=None):
        """
        Generates appropriate names for uploading a file to a 'dir' datastore.
        Actual work is done by _upload_to_storage.

        Parameters
        -----------
        storage: str
            Name of storage to upload the file into.
        vmid: int
            ID of the VM to associate the file with. This is enforced by
            Proxmox.
        filename: str
            Local filename of the file.
        disk_format: raw or qcow2
            Format of the file. The source type doesn't matter, as we will call
            `qemu-img` to both transfer and convert the file into the disk.
        disk_label: str
            Label to incorporate in the resulting disk name.
        disk_size: int
            Override the disk size. If not specified, the size is calculated
            from the file. In kilobytes.

        Returns
        -------
        Full canonical name of the disk.
        """
        ssh_session = self.client._backend.session
        diskname = "vm-{0}-{1}.{2}".format(vmid, disk_label, disk_format)
        storagename = "{0}:{1}/{2}".format(storage, vmid, diskname)

        self._upload_to_storage(ssh_session, storage, vmid, filename,
                                diskname, storagename, disk_format=disk_format,
                                disk_size=disk_size)

        return storagename

    def _upload_to_lvm_storage(self, storage, vmid, filename, disk_format,
                               disk_label, disk_size=None):
        """
        Generates appropriate names for uploading a file to a 'lvm' datastore.
        Actual work is done by _upload_to_storage.

        Parameters
        -----------
        storage: str
            Name of storage to upload the file into.
        vmid: int
            ID of the VM to associate the file with. This is enforced by
            Proxmox.
        filename: str
            Local filename of the file.
        disk_format: raw or qcow2
            Format of the file. Will be overridden into 'raw', because LVM only
            supports RAW disks.
        disk_label: str
            Label to incorporate in the resulting disk name.
        disk_size: int
            Override the disk size. If not specified, the size is calculated
            from the file. In kilobytes.

        Returns
        -------
        Full canonical name of the disk.
        """
        ssh_session = self.client._backend.session
        diskname = "vm-{0}-{1}".format(vmid, disk_label)
        storagename = "{0}:{1}".format(storage, diskname)

        # LVM only supports raw disks, overwrite the disk_format here.
        self._upload_to_storage(ssh_session, storage, vmid, filename,
                                diskname, storagename, disk_format="raw",
                                disk_size=disk_size)

        return storagename

    def upload(self, node, storage, vmid, filename, disk_format, disk_label,
               disk_size=None):
        """
        Upload a file into a datastore.

        Note that we can't yet upload a file to another node, only to the local
        node that we have an SSH connection with.

        Parameters
        ----------
        node: str
            Name of the node to upload to. See the note above.
        storage: str
            Name of storage to upload the file into.
        vmid: int
            ID of the VM to associate the file with. This is enforced by
            Proxmox.
        filename: str
            Local filename of the file.
        disk_format: raw or qcow2
            Format of the file.
        disk_label: str
            Label to incorporate in the resulting disk name.
        disk_size: int
            Override the disk size. If not specified, the size is calculated
            from the file. In kilobytes.
        """
        _node = self.client.nodes(node)
        _storage = _node.storage(storage)
        _type = _storage.status.get()['type']
        if _type == "dir":
            diskname = self._upload_to_flat_storage(
                storage=storage, vmid=vmid, filename=filename,
                disk_label=disk_label, disk_format=disk_format,
                disk_size=disk_size)
        elif _type == "lvm":
            diskname = self._upload_to_lvm_storage(
                storage=storage, vmid=vmid, filename=filename,
                disk_label=disk_label, disk_format=disk_format,
                disk_size=disk_size)
        else:
            raise ValueError(
                "Only dir and lvm storage are supported at this time")
        return diskname

    def attach_seed_iso(self, node, storage, vmid, iso_file):
        """
        Upload a cloud-init seed ISO file, and attach it to a VM.

        Parameters
        ----------
        node: str
            Name of the node to upload to. See the note above.
        storage: str
            Name of storage to upload the file into.
        vmid: int
            ID of the VM to associate the file with. This is enforced by
            Proxmox.
        iso_file: str
            Local filename of the ISO file.
        """
        _node = self.client.nodes(node)
        diskname = self.upload(node, storage, vmid, iso_file,
                               disk_label="cloudinit-seed", disk_format="raw")
        _node.qemu(vmid).config.set(virtio1=diskname)

    def attach_base_disk(self, node, storage, vmid, img_file, disk_size):
        """
        Upload a Cloud base image, and attach it to a VM.

        Parameters
        ----------
        node: str
            Name of the node to upload to. See the note above.
        storage: str
            Name of storage to upload the file into.
        vmid: int
            ID of the VM to associate the file with. This is enforced by
            Proxmox.
        img_file: str
            Local filename of the ISO file.
        disk_size: int
            Size of the disk to allocate, in kilobytes.
        """
        _node = self.client.nodes(node)
        diskname = self.upload(node, storage, vmid, img_file,
                               disk_label="base-disk", disk_format="qcow2",
                               disk_size=disk_size)
        _node.qemu(vmid).config.set(virtio0=diskname, bootdisk="virtio0")
