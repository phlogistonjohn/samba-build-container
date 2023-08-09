#!/usr/bin/python3

import argparse
import pathlib
import shlex
import subprocess


BUILDAH = "buildah"


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
    return subprocess.run(cmd, *args, **kwargs)


class ContainerVolume:
    def __init__(self, host_dir, dest_dir, flags=""):
        self.host_dir = host_dir
        self.dest_dir = dest_dir
        self.flags = flags or "rw,z"


class RunTask:
    def __init__(self, cmd, *, volumes=None):
        self.cmd = cmd
        self.volumes = volumes or []


class CopyTask:
    def __init__(self, sources, dest):
        self.sources = sources
        self.dest = dest


class _DNFTask(RunTask):
    def __init__(self, packages, dnf_cache=None):
        self._packages = packages
        self._dnf_cache = dnf_cache

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
        return ["dnf", "builddep", "-y"] + list(self._packages)


class BuildahBackend:
    def __init__(self, buildah_path=None, img=None):
        self._buildah = buildah_path or BUILDAH
        self._base = ""
        self._tasks = []
        self._img = img

    def base_image(self, image):
        self._base = image

    def add_build_task(self, task):
        self._tasks.append(task)

    def _commit(self):
        assert self._base
        res = _run(
            [self._buildah, "from", self._base],
            capture_output=True,
            check=True,
        )
        cid = res.stdout.strip().decode("utf8")
        try:
            for task in self._tasks:
                self._do_task(task, cid)
                children = getattr(task, "child_tasks", [])
                for child_task in children:
                    self._do_task(task, cid)
            if self._img:
                _run([self._buildah, "commit", cid, self._img])
        finally:
            _run([self._buildah, "rm", cid], check=True)

    def _do_task(self, task, cid):
        # TODO? consider switching to singledispatch
        if isinstance(task, RunTask):
            cmd = [self._buildah]
            vols = getattr(task, "volumes", [])
            for vol in vols:
                cmd.append(
                    f"--volume={vol.host_dir}:{vol.dest_dir}:{vol.flags}"
                )
            cmd.extend(["run", cid])
            cmd.extend(task.cmd)
            return _run(cmd, check=True)
        if isinstance(task, CopyTask):
            cmd = [self._buildah, "copy", cid]
            cmd.extend(task.sources)
            cmd.append(task.dest)
            return _run(cmd, check=True)
        raise TypeError("unexpected task type")

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, tb):
        if not exc_type:
            self._commit()


def cmd_build_image(cli, is_centos=False):
    build_dir = "/srv/dest"
    pkg_sources_dir = "/usr/local/lib/sources"
    with BuildahBackend(img=cli.working_image) as builder:
        builder.base_image(cli.base_image)
        builder.add_build_task(
            RunTask(
                [
                    "mkdir",
                    "-p",
                    "-m",
                    "0777",
                    build_dir,
                    pkg_sources_dir,
                ]
            )
        )
        builder.add_build_task(
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
                pkg_sources_dir,
            )
        )
        pkgs = [
            "git",
            "rsync",
            "gcc",
            "/usr/bin/rpmbuild",
            "dnf-command(builddep)",
        ]
        if is_centos:
            pkgs.append("epel-release")
            pkgs.append("centos-release-gluster")
            pkgs.append("centos-release-ceph")
        builder.add_build_task(DNFInstallTask(pkgs, cli.dnf_cache))
        spec = pathlib.Path(pkg_sources_dir) / "samba-master.spec"
        builder.add_build_task(DNFBuildDepTask([str(spec)], cli.dnf_cache))


def cmd_build_srpm(cli):
    src_dir = pathlib.Path("/build")
    build_dir = pathlib.Path("/srv/dest")
    pkg_sources_dir = pathlib.Path("/usr/local/lib/sources")
    spec = pkg_sources_dir / "samba-master.spec"
    working_spec = build_dir / "samba.spec"
    rpm_version = "4.999"
    working_tar = build_dir / f"samba-{rpm_version}.tar.gz"
    with BuildahBackend() as builder:
        builder.base_image(cli.working_image)
        volumes = [
            ContainerVolume(str(cli.source_dir), src_dir),
        ]
        if cli.artifacts_dir:
            volumes.append(
                ContainerVolume(str(cli.artifacts_dir), build_dir),
            )
        builder.add_build_task(
            RunTask(
                [
                    "rsync",
                    "-r",
                    f"{pkg_sources_dir}/",
                    f"{build_dir}/",
                ],
                volumes=volumes,
            )
        )
        builder.add_build_task(
            RunTask(
                [
                    "cp",
                    str(spec),
                    str(working_spec),
                ],
                volumes=volumes,
            )
        )
        builder.add_build_task(
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
                volumes=volumes,
            )
        )
        builder.add_build_task(
            RunTask(
                [
                    "rpmbuild",
                    "--define",
                    f"_topdir {build_dir}",
                    "--define",
                    f"_sourcedir {build_dir}",
                    "--define",
                    f"_srcrpmdir {build_dir}",
                    "-bs",
                    str(working_spec),
                ],
                volumes=volumes,
            )
        )


def cmd_build_rpm(cli):
    src_dir = pathlib.Path("/build")
    build_dir = pathlib.Path("/srv/dest")
    pkg_sources_dir = pathlib.Path("/usr/local/lib/sources")
    spec = pkg_sources_dir / "samba-master.spec"
    working_spec = build_dir / "samba.spec"
    rpm_version = "4.999"
    rpm_revision = "1"
    rpm_dist = ".fc38"
    working_srpm = (
        build_dir / f"samba-{rpm_version}-{rpm_revision}{rpm_dist}.src.rpm"
    )
    with BuildahBackend() as builder:
        builder.base_image(cli.working_image)
        volumes = [
            ContainerVolume(str(cli.source_dir), str(src_dir)),
        ]
        if cli.artifacts_dir:
            volumes.append(
                ContainerVolume(str(cli.artifacts_dir), str(build_dir)),
            )
        builder.add_build_task(
            RunTask(
                [
                    "rpmbuild",
                    "--define",
                    f"_topdir {build_dir}",
                    "--rebuild",
                    str(working_srpm),
                ],
                volumes=volumes,
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
        "--job",
        "-j",
        help="Name of path in working dir to write results",
    )
    parser.add_argument(
        "--install-deps-from",
        help="Installs dependencies based on a path to a SPEC file or SRPM",
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
        help="Even if a repo already exists try to checkout the supplied git ref",
    )
    parser.add_argument(
        "--with-ceph",
        action="store_true",
        help="Enable building Ceph components",
    )
    parser.add_argument(
        "--backend",
        choices=[BUILDAH],
        default=BUILDAH,
        help="Specify continer-build backend",
    )
    parser.add_argument(
        "--source-dir", "-s", type=host_path, help="Path to samba git checkout"
    )
    parser.add_argument(
        "--artifacts-dir",
        "-a",
        type=host_path,
        help="Path to a local directory where output will be saved",
    )
    parser.add_argument(
        "--base-image",
        default="registry.fedoraproject.org/fedora:38",
        help="Base image (example: quay.io/centos/centos:stream9)",
    )
    parser.add_argument(
        "--task",
        "-t",
        action="append",
        choices=("image", "srpm", "packages"),  # TODO: "configure", "make"
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
        help="Name or tag of builder image",
    )
    # TODO
    # parser.add_argument(
    #     "--example-yaml",
    #     action="store_true",
    #     help="Display example configuration yaml",
    # )
    parser.add_argument("--config", "-c", help="Provide a configuration file")
    cli = parser.parse_args()
    return cli


def main():
    cli = parse_cli()
    tasks = cli.task or []
    if not tasks:
        tasks = ["image", "srpm", "packages"]
    if "image" in tasks:
        cmd_build_image(cli)
    if "srpm" in tasks:
        cmd_build_srpm(cli)
    if "packages" in tasks:
        cmd_build_rpm(cli)


if __name__ == "__main__":
    main()
