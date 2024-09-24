#!/usr/bin/python3

import argparse
import enum
import hashlib
import json
import logging
import os
import pathlib
import shlex
import subprocess
import sys


log = logging.getLogger()


BASE_IMAGE = "registry.fedoraproject.org/fedora:40"
BUILDAH = "buildah"

ANN_BC = "us.asynchrono.samba-build-container"
ANN_SPEC_DIGEST = "us.asynchrono.samba-spec-digest"


def _host_path(value):
    return str(pathlib.Path(value).resolve())


def _working_image(img):
    if ":" in img:
        return img
    if img:
        return f"samba-builder:{img}"
    return "samba-builder:dev"


def _arg_str(value):
    if _to_arg := getattr(value, "to_arg", None):
        return _to_arg()
    if isinstance(value, pathlib.Path):
        return str(value)
    return value


def _cmdstr(cmd):
    return " ".join(shlex.quote(c) for c in cmd)


def _run(cmd, *args, **kwargs):
    cmd = [_arg_str(a) for a in cmd]
    log.info("Executing command: %s", _cmdstr(cmd))
    return subprocess.run(cmd, *args, **kwargs)


class Steps(enum.StrEnum):
    DNF_CACHE = "dnfcache"
    BUILD_CONTAINER = "build-container"
    CONTAINER = "container"
    CONFIGURE = "configure"
    MAKE = "make"
    SOURCE_RPM = "source-rpm"
    RPM = "rpm"
    OTHER = "other"


class ImageSource(enum.StrEnum):
    CACHE = "cache"
    PULL = "pull"
    BUILD = "build"


class ImageSourceAction(argparse.Action):
    def __init__(self, **kwargs):
        _choices = [e.value for e in ImageSource]
        kwargs['choices'] = _choices
        super().__init__(**kwargs)

    def __call__(self, parser, namespace, values, option_string=None):
        lst = getattr(namespace, self.dest, None) or []
        lst.append(ImageSource(values))
        setattr(namespace, self.dest, lst)


class ArgumentParser(argparse.ArgumentParser):
    def parse_my_args(self, args=None, namespace=None):
        args = sys.argv[1:] if args is None else list(args)
        if "--" in args:
            idx = args.index("--")
            my_args, rest = args[:idx], args[idx + 1 :]
        else:
            my_args, rest = args, []
        return self.parse_args(my_args, namespace=namespace), rest


class Builder:
    """Organize and manage the build steps."""

    _steps = {}

    def __init__(self):
        self._did_steps = set()

    def wants(self, step, ctx, *, force=False, top=False):
        log.info("want to execute build step: %s", step)
        if ctx.cli.no_prereqs and not top:
            log.info("Running prerequisite steps disabled")
            return
        if step in self._did_steps:
            log.info("step already done: %s", step)
            return
        self._steps[step](ctx)
        self._did_steps.add(step)
        log.info("step done: %s", step)

    def available_steps(self):
        return [str(k) for k in self._steps]

    @classmethod
    def set(self, step):
        def wrap(f):
            self._steps[step] = f
            f._for_step = step
            return f

        return wrap


class Context:
    """Command context."""

    def __init__(self, cli):
        self.cli = cli
        self._spec_file = pathlib.Path("samba-master.spec")
        self._default_spec_file = True
        if cli.rpm_spec_file:
            self._spec_file = pathlib.Path(cli.rpm_spec_file)
            self._default_spec_file = False
        self._image_paths = ImagePaths()

    @property
    def spec_file(self):
        return self._spec_file

    @property
    def is_default_spec_file(self):
        return self._default_spec_file

    @property
    def ipaths(self):
        return self._image_paths

    def volumes(self):
        volumes = []
        if self.cli.source_dir:
            volumes.append(
                ContainerVolume(
                    str(self.cli.source_dir), self.ipaths.src_dir
                ),
            )
        if self.cli.artifacts_dir:
            volumes.append(
                ContainerVolume(
                    str(self.cli.artifacts_dir), self.ipaths.build_dir
                ),
            )
        return volumes


class ContainerVolume:
    def __init__(self, host_dir, dest_dir, flags=""):
        self.host_dir = host_dir
        self.dest_dir = dest_dir
        self.flags = flags or "rw,z"

    @property
    def dest_dir_path(self):
        return pathlib.Path(self.dest_dir)

    def to_arg(self):
        return f"--volume={self.host_dir}:{self.dest_dir}:{self.flags}"


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
    def __init__(self, *, buildah_path=None, img=None, base="", volumes=None):
        self._buildah = buildah_path or BUILDAH
        self._base = base
        self._tasks = []
        self._img = img
        self._annotations = {}
        self._volumes = []
        if volumes:
            self.add_volumes(volumes)

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

    def direct(self, cmd, check=True, capture_output=True):
        cmd = [self._buildah] + cmd
        return _run(cmd, check=check, capture_output=capture_output)

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
        cmd.extend(self._with_volumes(task))
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
        self.spec = self.pkg_sources_dir / "samba.spec"


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


@Builder.set(Steps.BUILD_CONTAINER)
def build_container(ctx):
    #ctx.build.wants(Steps.DNF_CACHE, ctx)
    ipaths = ImagePaths()
    spec_file = ctx.spec_file
    cli = ctx.cli
    spec_digest = sha256_digest(spec_file)
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
                    "smb.conf.example",
                    "pam_winbind.conf",
                    "samba.logrotate",
                    "smb.conf.vendor",
                ],
                str(ipaths.pkg_sources_dir),
            )
        )
        builder.append(
            CopyTask([spec_file], ipaths.spec)
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
            pkgs.append("centos-release-ceph-reef")
            extra_repos.append("centos-ceph-reef")
            extra_repos.append('crb')
        builder.append(DNFInstallTask(pkgs, cli.dnf_cache))
        builder.append(DNFBuildDepTask([str(ipaths.spec)], cli.dnf_cache, enable_repos=extra_repos))


@Builder.set(Steps.CONTAINER)
def get_container(ctx):
    """Acquire an image that we will build in."""
    image_name = ctx.cli.working_image
    inspect_cmd = ["inspect", image_name]
    pull_cmd = ["pull", image_name]
    builder = BuildahBackend()

    allowed = ctx.cli.image_sources or ImageSource
    if ImageSource.CACHE in allowed:
        res = builder.direct(inspect_cmd)
        if res.returncode == 0:
            log.info("Container image %s present", image_name)
            return
        log.info("Container image %s not present", image_name)
    if ImageSource.PULL in allowed:
        res = builder.direct(pull_cmd)
        if res.returncode == 0:
            log.info("Container image %s pulled successfully", image_name)
            return
    log.info("Container image %s needed", image_name)
    if ImageSource.BUILD in allowed:
        ctx.build.wants(Steps.BUILD_CONTAINER, ctx)
        return
    raise ValueError("no available image sources")


def _version_read(builder, path, *, package=False, volumes=None):
    cmd = [
        "rpm",
        '-q',
        '--queryformat',
        '%{name}: %{version}\\n',
        '--package' if package else '--specfile',
        path,
    ]
    res = builder.immediate(RunTask(cmd, volumes=volumes))
    contents = dict(
        tuple(map(str.strip, l.split(':', 1)))
        for l in res.stdout.decode('utf8').splitlines()
    )
    return contents["samba"]


@Builder.set(Steps.SOURCE_RPM)
def cmd_build_srpm(ctx):
    ctx.build.wants(Steps.CONTAINER, ctx)
    spec_file = ctx.spec_file
    log.info("Using %s spec file", "default" if ctx.is_default_spec_file else "custom")
    spec_digest = sha256_digest(spec_file)
    log.info("Spec file digest: %s", spec_digest)
    volumes = ctx.volumes()
    if ctx.is_default_spec_file:
        ctr_spec_file = ctx.ipaths.spec
    else:
        ctr_spec_file = "/tmp/samba.spec"
        volumes.append(ContainerVolume(spec_file.absolute(), ctr_spec_file))
    with BuildahBackend(base=ctx.cli.working_image, volumes=volumes) as builder:
        rpm_version = _version_read(builder, ctr_spec_file)
        log.info("Samba RPM SPEC version: %s", rpm_version)
        check_digest = (ctx.is_default_spec_file
                        and ANN_BC in img_annotations
                        and ANN_SPEC_DIGEST in img_annotations)
        if check_digest:
            log.info("Checking spec file digest matches")
            prefixed_digest = f"sha256:{spec_digest}"
            if img_annotations[ANN_SPEC_DIGEST] != prefixed_digest:
                raise ValueError(
                    "spec digest mismatch: "
                    f"{prefixed_digest} != {img_annotations[ANN_SPEC_DIGEST]}"
                )
        build_dir = ctx.ipaths.build_dir
        # copy in build (other) files to build dir
        builder.append(
            RunTask(
                [
                    "rsync",
                    "-r",
                    f"{ctx.ipaths.pkg_sources_dir}/",
                    f"{build_dir}/",
                ],
            )
        )
        # copy in spec file to build dir
        working_spec = build_dir / "samba.spec"
        builder.append(
            RunTask(
                [
                "cp",
                ctr_spec_file,
                ctx.ipaths.spec,
                ]
            )
        )
        # generate source tarball from git tree
        working_tar = build_dir / f"samba-{rpm_version}.tar.gz"
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
        # generate SRPM in build dir
        builder.append(
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
            )
        )
    return


@Builder.set(Steps.RPM)
def cmd_build_rpm(ctx):
    ctx.build.wants(Steps.SOURCE_RPM, ctx)
    volumes = ctx.volumes()
    srpm_pat = f"samba-*.src.rpm"
    with BuildahBackend(base=ctx.cli.working_image, volumes=volumes) as builder:
        build_dir = ctx.ipaths.build_dir
        res = builder.immediate(
            RunTask(
                ["find", str(build_dir), "-name", srpm_pat],
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
                    f"_topdir {build_dir}",
                    "--rebuild",
                    working_srpm,
                ],
            )
        )


@Builder.set(Steps.CONFIGURE)
def cmd_configure(ctx):
    ctx.build.wants(Steps.CONTAINER, ctx)
    volumes = ctx.volumes()
    with BuildahBackend(base=ctx.cli.working_image, volumes=volumes) as builder:
        builder.append(
            RunTask(
                [
                    "./configure",
                    "--enable-developer",
                    "--enable-ceph-reclock",
                    "--with-cluster-support",
                ],
                workingdir=ctx.ipaths.src_dir,
            )
        )


@Builder.set(Steps.MAKE)
def cmd_make(ctx):
    ctx.build.wants(Steps.CONTAINER, ctx)
    volumes = ctx.volumes()
    with BuildahBackend(base=ctx.cli.working_image, volumes=volumes) as builder:
        builder.append(
            RunTask(
                ["make", "-j8"],
                workingdir=ctx.ipaths.src_dir,
            )
        )


@Builder.set(Steps.OTHER)
def cmd_other(ctx):
    if not ctx.cli.other:
        msg = "no additional arguments found"
        log.error(msg)
        log.error("Specify additional arguments after '--' on the command line")
        raise ValueError(msg)

    ctx.build.wants(Steps.CONTAINER, ctx)
    volumes = ctx.volumes()
    with BuildahBackend(base=ctx.cli.working_image, volumes=volumes) as builder:
        builder.append(
            RunTask(
                list(ctx.cli.other),
                workingdir=ctx.ipaths.src_dir,
            )
        )


def parse_cli(build_step_names):
    parser = ArgumentParser(
        description="""
Automate building samba packages using a container.

It can be invoked using only command line options or using a YAML based
configuration file. Use --help-yaml to see an example.
"""
    )
    parser.add_argument(
        "--debug", action="store_true", help="Emit debug logging",
    )
    parser.add_argument(
        "--cwd",
        help="Change working directory before executing commands",
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
        type=_host_path,
        help="Path to samba git checkout",
    )
    parser.add_argument(
        "--artifacts-dir",
        "-a",
        type=_host_path,
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
        "--image-sources",
        "-I",
        action=ImageSourceAction,
        help="Allowed sources for builder image"
    )
    parser.add_argument(
        "--rpm-spec-file",
        help="To RPM spec file",
    )
    parser.add_argument("--config", "-c", help="Provide a configuration file")
    parser.add_argument(
        "--no-prereqs",
        "-P",
        action="store_true",
        help="Do not execute any prerequisite steps. Only execute specified steps",
    )
    parser.add_argument(
        "--execute",
        "-e",
        dest="steps",
        action="append",
        choices=build_step_names,
        help="Execute the target build step(s)",
    )
    cli, other = parser.parse_my_args()
    cli.other = other
    return cli


def _src_root():
    return pathlib.Path(__file__).parent.absolute()


def _setup_logging(cli):
    level = logging.DEBUG if cli.debug else logging.INFO
    logger = logging.getLogger()
    logger.setLevel(level)
    handler = logging.StreamHandler()
    handler.setFormatter(
        logging.Formatter("{asctime}: {levelname}: {message}", style="{")
    )
    handler.setLevel(level)
    logger.addHandler(handler)


def main():
    builder = Builder()
    cli = parse_cli(builder.available_steps())
    _setup_logging(cli)

    os.chdir(cli.cwd or _src_root())
    ctx = Context(cli)
    ctx.build = builder
    for step in cli.steps or [Steps.BUILD]:
        ctx.build.wants(step, ctx, top=True)


if __name__ == "__main__":
    main()
