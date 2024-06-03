#!/usr/bin/python3

import argparse
import hashlib
import json
import pathlib
import shlex
import subprocess
import sys


BASE_IMAGE = "registry.fedoraproject.org/fedora:40"
BUILDAH = "buildah"

ANN_BC = "us.asynchrono.samba-build-container"
ANN_SPEC_DIGEST = "us.asynchrono.samba-spec-digest"


def host_path(value):
    return str(pathlib.Path(value).resolve())


def _working_image(img):
    if ":" in img:
        return img
    if img:
        return f"samba-builder:{img}"
    return "samba-builder:dev"


def _run(cmd, *args, **kwargs):
    print("--->", " ".join([shlex.quote(a) for a in cmd]))
    sys.stdout.flush()
    return subprocess.run(cmd, *args, **kwargs)


class ContainerVolume:
    def __init__(self, host_dir, dest_dir, flags=""):
        self.host_dir = host_dir
        self.dest_dir = dest_dir
        self.flags = flags or "rw,z"

    @property
    def dest_dir_path(self):
        return pathlib.Path(self.dest_dir)


class RunTask:
    def __init__(self, cmd, *, volumes=None, workingdir=None):
        self.cmd = cmd
        self.volumes = volumes or []
        self.workingdir = workingdir


class CopyTask:
    def __init__(self, sources, dest):
        self.sources = sources
        self.dest = dest


class _DNFTask(RunTask):
    def __init__(self, packages, dnf_cache=None, *, enable_repos=None):
        self._packages = packages
        self._dnf_cache = dnf_cache
        self._enable_repos = enable_repos

    @property
    def volumes(self):
        if not self._dnf_cache:
            return []
        cdir = pathlib.Path(self._dnf_cache)
        libdir = cdir / "lib"
        cachedir = cdir / "cache"
        libdir.mkdir(parents=True, exist_ok=True)
        cachedir.mkdir(parents=True, exist_ok=True)
        return [
            ContainerVolume(str(libdir), "/var/lib/dnf"),
            ContainerVolume(str(cachedir), "/var/cache/dnf"),
        ]


class DNFInstallTask(_DNFTask):
    @property
    def cmd(self):
        return ["dnf", "install", "-y"] + list(self._packages)


class DNFBuildDepTask(_DNFTask):
    @property
    def cmd(self):
        cmd = ["dnf", "builddep", "-y", ]
        if self._enable_repos:
            cmd.extend(f'--enablerepo={k}' for k in self._enable_repos)
        cmd.extend(self._packages)
        return cmd


class BuildahBackend:
    def __init__(self, buildah_path=None, img=None):
        self._buildah = buildah_path or BUILDAH
        self._base = ""
        self._tasks = []
        self._img = img
        self._annotations = {}
        self._volumes = []

    @property
    def base_image(self):
        return self._base

    @base_image.setter
    def base_image(self, image):
        self._base = image

    @property
    def annotations(self):
        return self._annotations

    def append(self, task):
        self._tasks.append(task)

    def immediate(self, task, check=True, capture_output=True):
        cid = self._new()
        try:
            cmd = self._task_to_cmd(task, cid)
            result = _run(cmd, check=check, capture_output=capture_output)
        finally:
            self._rm(cid)
        return result

    def add_volumes(self, volumes):
        if isinstance(volumes, ContainerVolume):
            volumes = [volumes]
        self._volumes.extend(volumes)

    def _with_volumes(self, task):
        volumes = list(self._volumes)
        volumes.extend(getattr(task, 'volumes', []))
        return volumes

    def _new(self):
        assert self._base
        res = _run(
            [self._buildah, "from", self._base],
            capture_output=True,
            check=True,
        )
        cid = res.stdout.strip().decode("utf8")
        return cid

    def _commit(self, cid):
        assert self._img
        if self._annotations:
            acmd = [self._buildah, "config"]
            for key, value in self._annotations.items():
                acmd.append(f"-a{key}={value}")
            acmd.append(cid)
            _run(acmd)
        _run([self._buildah, "commit", cid, self._img])

    def _rm(self, cid):
        _run([self._buildah, "rm", cid], check=True)

    def _apply(self):
        cid = self._new()
        try:
            for task in self._tasks:
                self._do_task(task, cid)
                children = getattr(task, "child_tasks", [])
                for child_task in children:
                    self._do_task(task, cid)
            if self._img:
                self._commit(cid)
        finally:
            self._rm(cid)

    def _do_task(self, task, cid):
        if isinstance(task, RunTask):
            return self._do_run_task(task, cid)
        if isinstance(task, CopyTask):
            return self._do_copy_task(task, cid)
        raise TypeError("unexpected task type")

    def _task_to_cmd(self, task, cid):
        cmd = [self._buildah]
        vols = self._with_volumes(task)
        for vol in vols:
            cmd.append(f"--volume={vol.host_dir}:{vol.dest_dir}:{vol.flags}")
        wdir = getattr(task, "workingdir", None)
        if wdir:
            cmd.append(f"--workingdir={wdir}")
        cmd.extend(["run", cid])
        cmd.extend(task.cmd)
        return cmd

    def _do_run_task(self, task, cid):
        cmd = self._task_to_cmd(task, cid)
        return _run(cmd, check=True)

    def _do_copy_task(self, task, cid):
        cmd = [self._buildah, "copy", cid]
        for vol in self._with_volumes(task):
            if pathlib.Path(task.dest).is_relative_to(vol.dest_dir_path):
                raise ValueError(f'destination {task.dest} is in volume {vol.dest_dir}')
        cmd.extend(task.sources)
        cmd.append(task.dest)
        return _run(cmd, check=True)

    def get_annotations(self, img):
        result = _run(
            [self._buildah, "inspect", img], capture_output=True, check=True
        )
        data = json.loads(result.stdout.decode("utf8"))
        return data.get("ImageAnnotations", {})

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, tb):
        if not exc_type:
            self._apply()


class ImagePaths:
    def __init__(self, *, root=None):
        self._root = root or pathlib.Path("/")
        self.src_dir = self._root / "build"
        self.build_dir = self._root / "srv/dest"
        self.pkg_sources_dir = self._root / "usr/local/lib/sources"
        self.spec = self.pkg_sources_dir / "samba-master.spec"


def _common_volumes(cli, ipaths):
    volumes = []
    if cli.source_dir:
        volumes.append(
            ContainerVolume(str(cli.source_dir), ipaths.src_dir),
        )
    if cli.artifacts_dir:
        volumes.append(
            ContainerVolume(str(cli.artifacts_dir), ipaths.build_dir),
        )
    return volumes


def sha256_digest(path, bsize=4096):
    hh = hashlib.sha256()
    buf = bytearray(bsize)
    with open(path, "rb") as fh:
        while True:
            rlen = fh.readinto(buf)
            hh.update(buf[:rlen])
            if rlen < len(buf):
                break
    spec_digest = hh.hexdigest()
    return spec_digest


def _parse_os_release(txt):
    out = {}
    for line in txt.splitlines():
        if line.startswith('#'):
            continue
        key, val = line.split("=", 1)
        if val.startswith('"') and val.endswith('"'):
            val = val[1:-1]
        out[key] = val
    return out


def cmd_build_image(cli):
    ipaths = ImagePaths()
    spec_digest = sha256_digest("samba-master.spec")
    with BuildahBackend(img=cli.working_image) as builder:
        builder.base_image = cli.base_image
        res = builder.immediate(
            RunTask(["cat", "/etc/os-release"]), check=False
        )
        if res.returncode != 0:
            is_centos = False
        else:
            os_info = _parse_os_release(res.stdout.decode("utf8"))
            is_centos = os_info.get('ID').startswith('centos')

        builder.annotations[ANN_BC] = "true"
        builder.annotations[ANN_SPEC_DIGEST] = f"sha256:{spec_digest}"
        builder.append(
            RunTask(
                [
                    "mkdir",
                    "-p",
                    "-m",
                    "0777",
                    str(ipaths.build_dir),
                    str(ipaths.pkg_sources_dir),
                ]
            )
        )
        builder.append(
            CopyTask(
                [
                    "samba.pamd",
                    "README.downgrade",
                    "samba-master.spec",
                    "smb.conf.example",
                    "pam_winbind.conf",
                    "samba.logrotate",
                    "smb.conf.vendor",
                ],
                str(ipaths.pkg_sources_dir),
            )
        )
        pkgs = [
            "git",
            "rsync",
            "gcc",
            "/usr/bin/rpmbuild",
            "dnf-command(builddep)",
        ]
        extra_repos = []
        if is_centos:
            pkgs.append("epel-release")
            extra_repos.append("epel")
            pkgs.append("centos-release-gluster")
            extra_repos.append("centos-gluster11")
            pkgs.append("centos-release-ceph")
            extra_repos.append("centos-ceph-reef")
            extra_repos.append('crb')
        builder.append(DNFInstallTask(pkgs, cli.dnf_cache))
        builder.append(DNFBuildDepTask([str(ipaths.spec)], cli.dnf_cache, enable_repos=extra_repos))


def cmd_build_srpm(cli):
    spec_file = "samba-master.spec"
    custom_spec = False
    if cli.rpm_spec_file:
        spec_file = cli.rpm_spec_file
        custom_spec = True
        print('Using custom spec file')
    spec_digest = sha256_digest(spec_file)
    ipaths = ImagePaths()
    rpm_version = "4.999"
    working_tar = ipaths.build_dir / f"samba-{rpm_version}.tar.gz"
    working_spec = ipaths.build_dir / "samba.spec"
    volumes = _common_volumes(cli, ipaths)
    with BuildahBackend() as builder:
        print('hhhhh')
        img_annotations = builder.get_annotations(cli.working_image)
        if not custom_spec and ANN_BC in img_annotations and ANN_SPEC_DIGEST in img_annotations:
            prefixed_digest = f"sha256:{spec_digest}"
            if img_annotations[ANN_SPEC_DIGEST] != prefixed_digest:
                raise ValueError(
                    "spec digest mismatch: "
                    f"{prefixed_digest} != {img_annotations[ANN_SPEC_DIGEST]}"
                )
        builder.base_image = cli.working_image
        builder.add_volumes(volumes)
        builder.append(
            RunTask(
                [
                    "rsync",
                    "-r",
                    f"{ipaths.pkg_sources_dir}/",
                    f"{ipaths.build_dir}/",
                ],
            )
        )
        curr_spec = ipaths.spec
        if custom_spec:
            # dumb workaround
            curr_spec = f'/var/{spec_digest}'
            builder.append(
                CopyTask([spec_file], curr_spec)
            )
        builder.append(
            RunTask(
                [
                    "cp",
                    curr_spec,
                    str(working_spec),
                ],
            )
        )
        builder.append(
            RunTask(
                [
                    "git",
                    "-C",
                    "/build",
                    "archive",
                    f"--prefix=samba-{rpm_version}/",
                    f"--output={working_tar}",
                    "HEAD",
                ],
            )
        )
        builder.append(
            RunTask(["ls", "-l", str(ipaths.build_dir)])
        )

        builder.append(
            RunTask(
                [
                    "rpmbuild",
                    "--define",
                    f"_topdir {ipaths.build_dir}",
                    "--define",
                    f"_sourcedir {ipaths.build_dir}",
                    "--define",
                    f"_srcrpmdir {ipaths.build_dir}",
                    "-bs",
                    str(working_spec),
                ],
            )
        )


def cmd_build_rpm(cli):
    ipaths = ImagePaths()
    rpm_version = "4.999"
    srpm_pat = f"samba-{rpm_version}-*.src.rpm"
    volumes = _common_volumes(cli, ipaths)
    with BuildahBackend() as builder:
        builder.base_image = cli.working_image
        res = builder.immediate(
            RunTask(
                ["find", str(ipaths.build_dir), "-name", srpm_pat],
                volumes=volumes,
            )
        )
        found = res.stdout.decode("utf8").strip().splitlines()
        if len(found) != 1:
            raise ValueError("too many srpms found")
        working_srpm = found[0]
        builder.append(
            RunTask(
                [
                    "rpmbuild",
                    "--define",
                    f"_topdir {ipaths.build_dir}",
                    "--rebuild",
                    str(working_srpm),
                ],
                volumes=volumes,
            )
        )


def cmd_configure(cli):
    assert cli.source_dir
    ipaths = ImagePaths()
    volumes = _common_volumes(cli, ipaths)
    with BuildahBackend() as builder:
        builder.base_image = cli.working_image
        builder.append(
            RunTask(
                [
                    "./configure",
                    "--enable-developer",
                    "--enable-ceph-reclock",
                    "--with-cluster-support",
                ],
                volumes=volumes,
                workingdir=str(ipaths.src_dir),
            )
        )


def cmd_make(cli):
    assert cli.source_dir
    ipaths = ImagePaths()
    volumes = _common_volumes(cli, ipaths)
    with BuildahBackend() as builder:
        builder.base_image = cli.working_image
        builder.append(
            RunTask(
                ["make", "-j8"],
                volumes=volumes,
                workingdir=str(ipaths.src_dir),
            )
        )


def cmd_any(cli):
    assert cli.source_dir
    ipaths = ImagePaths()
    volumes = _common_volumes(cli, ipaths)
    with BuildahBackend() as builder:
        builder.base_image = cli.working_image
        builder.append(
            RunTask(
                list(cli.other),
                volumes=volumes,
                workingdir=str(ipaths.src_dir),
            )
        )


def parse_cli():
    parser = argparse.ArgumentParser(
        description="""
Automate building samba packages using a container.

It can be invoked using only command line options or using a YAML based
configuration file. Use --help-yaml to see an example.
"""
    )
    parser.add_argument(
        "--git-ref", default="master", help="Samba git ref to check out"
    )
    parser.add_argument(
        "--git-repo",
        default="https://git.samba.org/samba.git",
        help="Samba git repo",
    )
    parser.add_argument(
        "--force-ref",
        action="store_true",
        help=(
            "Even if a repo already exists try to checkout the supplied"
            " git ref"
        ),
    )
    parser.add_argument(
        "--with-ceph",
        action="store_true",
        help="Enable building Ceph components",
    )
    parser.add_argument(
        "--source-dir",
        "-s",
        type=host_path,
        help="Path to samba git checkout",
    )
    parser.add_argument(
        "--artifacts-dir",
        "-a",
        type=host_path,
        help="Path to a local directory where output will be saved",
    )
    parser.add_argument(
        "--base-image",
        default=BASE_IMAGE,
        help=f"Base image (example: {BASE_IMAGE})",
    )
    parser.add_argument(
        "--task",
        "-t",
        action="append",
        choices=("image", "srpm", "packages", "configure", "make", "cmd"),
        help="What to build",
    )
    parser.add_argument(
        "--dnf-cache",
        help="Path to a directory for caching dnf state",
    )
    parser.add_argument(
        "--working-image",
        "-w",
        type=_working_image,
        default="",
        help="Name or tag for builder image",
    )
    parser.add_argument(
        "--rpm-spec-file",
        help="To RPM spec file",
    )
    # TODO
    # parser.add_argument(
    #     "--example-yaml",
    #     action="store_true",
    #     help="Display example configuration yaml",
    # )
    parser.add_argument("--config", "-c", help="Provide a configuration file")
    parser.add_argument("other", nargs=argparse.REMAINDER)
    cli = parser.parse_args()
    return cli


def main():
    cli = parse_cli()
    tasks = cli.task or []
    if not tasks:
        tasks = ["image", "srpm", "packages"]
    if "image" in tasks:
        cmd_build_image(cli)
    if "configure" in tasks:
        cmd_configure(cli)
    if "make" in tasks:
        cmd_make(cli)
    if "srpm" in tasks:
        cmd_build_srpm(cli)
    if "packages" in tasks:
        cmd_build_rpm(cli)
    if "cmd" in tasks:
        cmd_any(cli)


if __name__ == "__main__":
    main()
