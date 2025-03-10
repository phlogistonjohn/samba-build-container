#!/usr/bin/python3
"""build-with-container.py - Build Samba in a Containerized environment.
"""

import argparse
import contextlib
import enum
import glob
import hashlib
import logging
import os
import pathlib
import shlex
import shutil
import subprocess
import sys
import tempfile
import urllib.request

log = logging.getLogger()


try:
    from enum import StrEnum
except ImportError:

    class StrEnum(str, enum.Enum):
        def __str__(self):
            return self.value


class DistroKind(StrEnum):
    CENTOS10 = "centos10"
    CENTOS8 = "centos8"
    CENTOS9 = "centos9"
    FEDORA41 = "fedora41"
    UBUNTU2204 = "ubuntu22.04"
    UBUNTU2404 = "ubuntu24.04"

    @classmethod
    def uses_dnf(cls):
        return {cls.CENTOS8, cls.CENTOS9, cls.CENTOS10, cls.FEDORA41}

    @classmethod
    def uses_rpmbuild(cls):
        # right now this is the same as uses_dnf, but perhaps not always
        # let's be specific in our interface
        return cls.uses_dnf()  # but lazy in the implementation

    @classmethod
    def uses_centos_repos(cls):
        return {cls.CENTOS8, cls.CENTOS9, cls.CENTOS10}

    @classmethod
    def aliases(cls):
        return {
            str(cls.CENTOS10): cls.CENTOS10,
            "centos10stream": cls.CENTOS10,
            str(cls.CENTOS8): cls.CENTOS8,
            str(cls.CENTOS9): cls.CENTOS9,
            "centos9stream": cls.CENTOS9,
            str(cls.FEDORA41): cls.FEDORA41,
            "fc41": cls.FEDORA41,
            str(cls.UBUNTU2204): cls.UBUNTU2204,
            "ubuntu-jammy": cls.UBUNTU2204,
            "jammy": cls.UBUNTU2204,
            str(cls.UBUNTU2404): cls.UBUNTU2404,
            "ubuntu-noble": cls.UBUNTU2404,
            "noble": cls.UBUNTU2404,
        }

    @classmethod
    def from_alias(cls, value):
        return cls.aliases()[value]


class DefaultImage(StrEnum):
    CENTOS10 = "quay.io/centos/centos:stream10"
    CENTOS8 = "quay.io/centos/centos:stream8"
    CENTOS9 = "quay.io/centos/centos:stream9"
    FEDORA41 = "registry.fedoraproject.org/fedora:41"
    UBUNTU2204 = "docker.io/ubuntu:22.04"
    UBUNTU2404 = "docker.io/ubuntu:24.04"


class CommandFailed(Exception):
    pass


class DidNotExecute(Exception):
    pass


class Shell(str):
    pass


def _cmdquote(cmd, *, conv=str):
    if isinstance(cmd, Shell):
        return str(cmd)
    return shlex.quote(str(cmd))


def _cmdstr(cmd):
    if isinstance(cmd, Shell):
        return str(cmd)
    return " ".join(_cmdquote(c) for c in cmd)


def _cmdchain(commands):
    return " && ".join(_cmdstr(args) for args in commands)


def _run(cmd, *args, **kwargs):
    ctx = kwargs.pop("ctx", None)
    if ctx and ctx.dry_run:
        log.info("(dry-run) Not Executing command: %s", _cmdstr(cmd))
        # because we can not return a result (as we did nothing)
        # raise a specific exception to be caught by higher layer
        raise DidNotExecute(cmd)

    log.info("Executing command: %s", _cmdstr(cmd))
    return subprocess.run(cmd, *args, **kwargs)


def _container_cmd(ctx, args, *, workdir=None, interactive=False, ports=None):
    rm_container = not ctx.cli.keep_container
    cmd = [
        ctx.container_engine,
        "run",
        "--name=samba_build",
    ]
    if interactive:
        cmd.append("-it")
    if rm_container:
        cmd.append("--rm")
    if "podman" in ctx.container_engine:
        cmd.append("--pids-limit=-1")
    if ctx.map_user:
        cmd.append("--user=0")
    if workdir:
        cmd.append(f"--workdir={workdir}")
    cwd = pathlib.Path(".").absolute()
    overlay = ctx.overlay()
    if overlay and overlay.temporary:
        cmd.append(f"--volume={ctx.cli.source_dir}:{ctx.cli.homedir}:O")
    elif overlay:
        cmd.append(
            f"--volume={ctx.cli.source_dir}:{ctx.cli.homedir}:O,upperdir={overlay.upper},workdir={overlay.work}"
        )
    else:
        cmd.append(f"--volume={ctx.cli.source_dir}:{ctx.cli.homedir}:Z")
    cmd.append(f"-eHOMEDIR={ctx.cli.homedir}")
    if ctx.cli.build_dir:
        cmd.append(f"-eBUILD_DIR={ctx.cli.build_dir}")
    if ctx.cli.ccache_dir:
        ccdir = str(ctx.cli.ccache_dir).format(
            homedir=ctx.cli.homedir or "",
            build_dir=ctx.cli.build_dir or "",
            distro=ctx.cli.distro or "",
        )
        cmd.append(f"-eCCACHE_DIR={ccdir}")
        cmd.append(f"-eCCACHE_BASEDIR={ctx.cli.homedir}")
    for port_req in (ports or []):
        if isinstance(port_req, str):
            cmd.append(f'--publish={port_req}')
        if isinstance(port_req, int):
            cmd.append(f'--publish={port_req}:{port_req}')
        else:
            raise ValueError('invalid port type: {port_req!r}')
    for extra_arg in ctx.cli.extra or []:
        cmd.append(extra_arg)
    cmd.append(ctx.image_name)
    cmd.extend(args)
    return cmd


def _git_command(ctx, args):
    cmd = ["git"]
    cmd.extend(args)
    return cmd


def _git_current_branch(ctx):
    cmd = _git_command(ctx, ["rev-parse", "--abbrev-ref", "HEAD"])
    res = _run(cmd, check=True, capture_output=True, cwd=ctx.cli.source_dir)
    return res.stdout.decode("utf8").strip()


def _git_current_sha(ctx, short=True):
    args = ["rev-parse"]
    if short:
        args.append("--short")
    args.append("HEAD")
    cmd = _git_command(ctx, args)
    res = _run(cmd, check=True, capture_output=True, cwd=ctx.cli.source_dir)
    return res.stdout.decode("utf8").strip()


class Steps(StrEnum):
    DNF_CACHE = "dnfcache"
    BUILD_CONTAINER = "build-container"
    CONTAINER = "container"
    CONFIGURE = "configure"
    BUILD = "build"
    CUSTOM = "custom"
    TARBALL = "tarball"
    SOURCE_RPM = "source-rpm"
    RPM = "rpm"
    PACKAGES = "packages"
    INTERACTIVE = "interactive"
    SYNC = "sync"


class ImageSource(StrEnum):
    CACHE = "cache"
    PULL = "pull"
    BUILD = "build"

    @classmethod
    def argument(cls, value):
        try:
            return {cls(v) for v in value.split(",")}
        except Exception:
            raise argparse.ArgumentTypeError(
                f"the argument must be one of {cls.hint()}"
                " or a comma delimited list of those values"
            )

    @classmethod
    def hint(cls):
        return ", ".join(s.value for s in cls)


class Context:
    """Command context."""

    def __init__(self, cli):
        self.cli = cli
        self._engine = None
        self.distro_cache_name = ""
        self.cache = argparse.Namespace(
            rpm_version=None,
            spec_sha256_sum="",
        )

    @property
    def container_engine(self):
        if self._engine is not None:
            return self._engine
        if self.cli.container_engine:
            return self.cli.container_engine

        for ctr_eng in ["podman", "docker"]:
            if shutil.which(ctr_eng):
                break
        else:
            raise RuntimeError("no container engine found")
        log.debug("found container engine: %r", ctr_eng)
        self._engine = ctr_eng
        return self._engine

    @property
    def image_name(self):
        base = self.cli.image_repo or "samba-build"
        return f"{base}:{self.target_tag()}"

    def target_tag(self):
        suffix = ""
        if self.cli.tag and self.cli.tag.startswith("+"):
            suffix = f".{self.cli.tag[1:]}"
        elif self.cli.tag:
            return self.cli.tag
        branch = self.cli.current_branch
        if not branch:
            try:
                branch = _git_current_branch(self).replace("/", "-")
            except subprocess.CalledProcessError:
                branch = "UNKNOWN"
        return f"{branch}.{self.cli.distro}{suffix}"

    def base_branch(self):
        # because git truly is the *stupid* content tracker there's not a
        # simple way to detect base branch. In BWC the base branch is really
        # only here for an optional 2nd level of customization in the build
        # container bootstrap we default to `main` even when that's not true.
        # One can explicltly set the base branch on the command line to invoke
        # customizations (that don't yet exist) or invalidate image caching.
        return self.cli.base_branch or "main"

    @property
    def from_image(self):
        if self.cli.base_image:
            return self.cli.base_image
        distro_images = {
            fld.value: getattr(DefaultImage, fld.name).value
            for fld in DistroKind
        }
        return distro_images[self.cli.distro]

    @property
    def dnf_cache_dir(self):
        if self.cli.dnf_cache_path and self.distro_cache_name:
            path = (
                pathlib.Path(self.cli.dnf_cache_path) / self.distro_cache_name
            )
            path = path.expanduser()
            return path.resolve()
        return None

    @property
    def map_user(self):
        # TODO: detect if uid mapping is needed
        return os.getuid() != 0

    @property
    def dry_run(self):
        return self.cli.dry_run

    @contextlib.contextmanager
    def user_command(self):
        """Handle subprocess execptions raised by commands we expect to be fallible.
        Helps hide traceback noise when just running commands.
        """
        try:
            yield
        except subprocess.SubprocessError as err:
            if self.cli.debug:
                raise
            raise CommandFailed() from err
        except DidNotExecute:
            pass

    def overlay(self):
        if not self.cli.overlay_dir:
            return None
        overlay = Overlay(temporary=self.cli.overlay_dir == "-")
        if not overlay.temporary:
            obase = pathlib.Path(self.cli.overlay_dir).resolve()
            # you can't nest the workdir inside the upperdir at least on the
            # version of podman I tried. But the workdir does need to be on the
            # same FS according to the docs.  So make the workdir and the upper
            # dir (content) siblings within the specified dir. podman doesn't
            # have the courtesy to manage the workdir automatically when
            # specifying upper dir.
            overlay.upper = obase / "content"
            overlay.work = obase / "work"
        return overlay


class Overlay:
    def __init__(self, temporary=True, upper=None, work=None):
        self.temporary = temporary
        self.upper = upper
        self.work = work


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
        if not self._did_steps:
            prepare_env_once(ctx)
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

    @classmethod
    def docs(cls):
        for step, func in cls._steps.items():
            yield str(step), getattr(func, "__doc__", "")


def prepare_env_once(ctx):
    overlay = ctx.overlay()
    if overlay and not overlay.temporary:
        log.info("Creating overlay dirs: %s, %s", overlay.upper, overlay.work)
        overlay.upper.mkdir(parents=True, exist_ok=True)
        overlay.work.mkdir(parents=True, exist_ok=True)


@Builder.set(Steps.DNF_CACHE)
def dnf_cache_dir(ctx):
    """Set up a DNF cache directory for reuse across container builds."""
    if ctx.cli.distro not in DistroKind.uses_dnf():
        return
    if not ctx.cli.dnf_cache_path:
        return

    ctx.distro_cache_name = f"_samba_{ctx.cli.distro}"
    cache_dir = ctx.dnf_cache_dir
    (cache_dir / "lib").mkdir(parents=True, exist_ok=True)
    (cache_dir / "cache").mkdir(parents=True, exist_ok=True)
    (cache_dir / ".DNF_CACHE").touch(exist_ok=True)


@Builder.set(Steps.BUILD_CONTAINER)
def build_container(ctx):
    """Generate a build environment container image."""
    ctx.build.wants(Steps.DNF_CACHE, ctx)
    cmd = [
        ctx.container_engine,
        "build",
        "--pull=always",
        "-t",
        ctx.image_name,
    ]
    cdir = pathlib.Path(ctx.cli.containerdir)
    spec_checksums = [
        f"{p.name}@{_sha256_digest(p)}"
        for p in (cdir / n for n in ("samba-master.spec",))
    ]
    cmd.append(f"--build-arg=SPECFILE_CHECKSUMS={' '.join(spec_checksums)}")
    if ctx.dnf_cache_dir and "docker" in ctx.container_engine:
        log.warning(
            "The --volume option is not supported by docker. Skipping dnf cache dir mounts"
        )
    elif ctx.dnf_cache_dir:
        cmd += [
            f"--volume={ctx.dnf_cache_dir}/lib:/var/lib/dnf:Z",
            f"--volume={ctx.dnf_cache_dir}:/var/cache/dnf:Z",
            "--build-arg=CLEAN_DNF=no",
        ]
    with tempfile.NamedTemporaryFile(mode="w+") as fp:
        _generate_samba_build_dockerfile(fp, ctx)
        fp.flush()
        os.fsync(fp.fileno())
        fp.seek(0)
        print("Containerfile:", fp.read())
        cmd += ["-f", fp.name, ctx.cli.containerdir]
        with ctx.user_command():
            _run(cmd, check=True, ctx=ctx)


def _sha256_digest(path, bsize=4096):
    hh = hashlib.sha256()
    buf = bytearray(bsize)
    with open(path, "rb") as fh:
        while True:
            rlen = fh.readinto(buf)
            hh.update(buf[:rlen])
            if rlen < len(buf):
                break
    spec_digest = hh.hexdigest()
    return "sha256:" + spec_digest


def _generate_samba_build_dockerfile(fh, ctx):
    build_dir = ctx.cli.homedir
    pkg_sources_dir = "/usr/local/src/samba"
    print(f"FROM {ctx.from_image}", file=fh)
    print(f"RUN mkdir -p -m 0777 {build_dir} {pkg_sources_dir}", file=fh)
    files = [
        "samba.pamd",
        "README.downgrade",
        "smb.conf.example",
        "pam_winbind.conf",
        "samba.logrotate",
        "smb.conf.vendor",
        "samba-master.spec",
    ]
    print(f'COPY {" ".join(files)} {pkg_sources_dir}', file=fh)
    commands = []
    repo_opts = []
    distro = DistroKind(ctx.cli.distro)
    if distro in DistroKind.uses_centos_repos():
        commands.append(
            [
                "dnf",
                "install",
                "-y",
                "epel-release",
                "centos-release-gluster",
                "centos-release-ceph-reef",
            ]
        )
        repo_opts = [
            f"--enablerepo={v}"
            for v in ("crb", "epel", "centos-gluster11", "centos-ceph-reef")
        ]
    commands.append(
        ["dnf", "install", "-y"]
        + repo_opts
        + [
            "git",
            "rsync",
            "gcc",
            "/usr/bin/rpmbuild",
            "dnf-command(builddep)",
        ]
    )
    commands.append(
        ["dnf", "builddep", "-y"]
        + repo_opts
        + [f"{pkg_sources_dir}/samba-master.spec"]
    )
    rpm_commands = _cmdchain(commands)
    print(f"RUN {rpm_commands}", file=fh)


@Builder.set(Steps.CONTAINER)
def get_container(ctx):
    """Build or fetch a container image that we will build in."""
    inspect_cmd = [
        ctx.container_engine,
        "image",
        "inspect",
        ctx.image_name,
    ]
    pull_cmd = [
        ctx.container_engine,
        "pull",
        ctx.image_name,
    ]
    allowed = ctx.cli.image_sources or ImageSource
    if ImageSource.CACHE in allowed:
        res = _run(inspect_cmd, check=False, capture_output=True)
        if res.returncode == 0:
            log.info("Container image %s present", ctx.image_name)
            return
        log.info("Container image %s not present", ctx.image_name)
    if ImageSource.PULL in allowed:
        res = _run(pull_cmd, check=False, capture_output=True)
        if res.returncode == 0:
            log.info("Container image %s pulled successfully", ctx.image_name)
            return
    log.info("Container image %s needed", ctx.image_name)
    if ImageSource.BUILD in allowed:
        ctx.build.wants(Steps.BUILD_CONTAINER, ctx)
        return
    raise ValueError("no available image sources")


@Builder.set(Steps.CONFIGURE)
def bc_configure(ctx):
    """Configure the build"""
    ctx.build.wants(Steps.CONTAINER, ctx)
    cmd = _container_cmd(
        ctx,
        [
            "bash",
            "-c",
            _cmdchain(
                [
                    ["cd", ctx.cli.homedir],
                    Shell("if [ -f .lock-wscript ]; then exit 0; fi"),
                    [
                        "./configure",
                        "--enable-developer",
                        "--enable-ceph-reclock",
                        "--with-cluster-support",
                    ],
                ]
            ),
        ],
    )
    with ctx.user_command():
        _run(cmd, check=True, ctx=ctx)


@Builder.set(Steps.BUILD)
def bc_build(ctx):
    """Execute a standard build."""
    ctx.build.wants(Steps.CONFIGURE, ctx)
    cmd = _container_cmd(
        ctx,
        [
            "bash",
            "-c",
            _cmdchain(
                [
                    ["cd", ctx.cli.homedir],
                    ["make", "-j"],
                ]
            ),
        ],
    )
    with ctx.user_command():
        _run(cmd, check=True, ctx=ctx)


def _update_rpm_info(ctx):
    pkg_sources_dir = "/usr/local/src/samba"
    spec_path = f"{pkg_sources_dir}/samba-master.spec"
    rpm_ver_info = [
        "rpm",
        "-q",
        "--queryformat",
        "%{name}: %{version}\\n",
        "--specfile",
        spec_path,
    ]
    res = _run(
        _container_cmd(ctx, rpm_ver_info), check=True, capture_output=True
    )
    contents = dict(
        tuple(map(str.strip, l.split(":", 1)))
        for l in res.stdout.decode("utf8").splitlines()
    )
    ctx.cache.rpm_version = contents["samba"]

    res = _run(
        _container_cmd(ctx, ["sha256sum", spec_path]),
        check=True,
        capture_output=True,
    )
    for line in res.stdout.decode("utf8").splitlines():
        if line.endswith(spec_path):
            sha256sum = line.split()[0]
            contents["sha256sum"] = sha256sum
            ctx.cache.spec_sha256_sum = sha256sum


@Builder.set(Steps.TARBALL)
def bc_make_tarball(ctx):
    """Build source tarball."""
    ctx.build.wants(Steps.CONTAINER, ctx)
    if not ctx.cache.rpm_version:
        _update_rpm_info(ctx)
    log.info("Samba RPM SPEC version: %s", ctx.cache.rpm_version)

    # generate source tarball from git tree
    ctr_commands = [
        ["cd", ctx.cli.homedir],
    ]
    tar_name = f"samba-{ctx.cache.rpm_version}.tar.gz"
    if ctx.cache.spec_sha256_sum:
        dname = _short(ctx.cache.spec_sha256_sum)
        tar_name = f"{dname}/{tar_name}"
        ctr_commands.append(["mkdir", "-p", dname])
    ctr_commands.append(
        [
            "git",
            "-C",
            ctx.cli.homedir,
            "archive",
            f"--prefix=samba-{ctx.cache.rpm_version}/",
            f"--output={tar_name}",
            "HEAD",
        ]
    )
    cmd = _container_cmd(
        ctx,
        [
            "bash",
            "-c",
            _cmdchain(ctr_commands),
        ],
    )
    with ctx.user_command():
        _run(cmd, check=True, ctx=ctx)


@Builder.set(Steps.SOURCE_RPM)
def bc_make_source_rpm(ctx):
    """Build SPRMs."""
    ctx.build.wants(Steps.TARBALL, ctx)
    # generate SRPM in build dir
    build_dir = ctx.cli.homedir
    if ctx.cache.spec_sha256_sum:
        build_dir = f"{ctx.cli.homedir}/{_short(ctx.cache.spec_sha256_sum)}"
    pkg_sources_dir = "/usr/local/src/samba"
    cmd = _container_cmd(
        ctx,
        [
            "bash",
            "-c",
            _cmdchain(
                [
                    [
                        "rsync",
                        "-r",
                        f"{pkg_sources_dir}/",
                        f"{build_dir}/",
                    ],
                    [
                        "cp",
                        f"{pkg_sources_dir}/samba-master.spec",
                        f"{build_dir}/samba.spec",  # canonical name
                    ],
                    [
                        "rpmbuild",
                        "--define",
                        f"_topdir {build_dir}",
                        "--define",
                        f"_sourcedir {build_dir}",
                        "--define",
                        f"_srcrpmdir {build_dir}",
                        "-bs",
                        f"{build_dir}/samba.spec",
                    ],
                ]
            ),
        ],
    )
    with ctx.user_command():
        _run(cmd, check=True, ctx=ctx)


@Builder.set(Steps.RPM)
def bc_build_rpm(ctx):
    """Build RPMs from SRPM."""
    ctx.build.wants(Steps.CONTAINER, ctx)
    if not ctx.cache.rpm_version:
        _update_rpm_info(ctx)
    log.info("Samba RPM SPEC version: %s", ctx.cache.rpm_version)

    build_dir = ctx.cli.homedir
    if ctx.cache.spec_sha256_sum:
        build_dir = f"{ctx.cli.homedir}/{_short(ctx.cache.spec_sha256_sum)}"
    srpm_glob = f"samba-{ctx.cache.rpm_version}*.src.rpm"
    check_cmd = _container_cmd(
        ctx,
        ["find", build_dir, "-name", srpm_glob],
    )
    res = _run(check_cmd, check=False, capture_output=True)
    paths = res.stdout.decode("utf8").splitlines()

    if len(paths) > 1:
        raise RuntimeError(
            "too many matching source rpms"
            f" (rename or remove unwanted files matching {srpm_glob} in the"
            " build dir and try again)"
        )
    if not paths:
        # no matches. build a new srpm
        ctx.build.wants(Steps.SOURCE_RPM, ctx)
        paths = glob.glob(srpm_glob)
        assert paths

    srpm_path = paths[0]
    topdir = pathlib.Path(ctx.cli.homedir) / "rpmbuild"
    cmd = _container_cmd(
        ctx,
        [
            "bash",
            "-c",
            _cmdchain(
                [
                    Shell("set -x"),
                    ["mkdir", "-p", topdir],
                    [
                        "rpmbuild",
                        f"-D_topdir {topdir}",
                        "--rebuild",
                        srpm_path,
                    ],
                ]
            ),
        ],
    )
    with ctx.user_command():
        _run(cmd, check=True, ctx=ctx)


@Builder.set(Steps.PACKAGES)
def bc_make_packages(ctx):
    """Build some sort of distro packages - chooses target based on distro."""
    if ctx.cli.distro in DistroKind.uses_rpmbuild():
        ctx.build.wants(Steps.RPM, ctx)
    else:
        raise NotImplementedError("not supported")


@Builder.set(Steps.CUSTOM)
def bc_custom(ctx):
    """Run a custom build command."""
    ctx.build.wants(Steps.CONTAINER, ctx)
    if not ctx.cli.remaining_args:
        raise RuntimeError(
            "no command line arguments provided:"
            " specify command after '--' on the command line"
        )
    cc = " ".join(ctx.cli.remaining_args)
    log.info("Custom command: %r", cc)
    cmd = _container_cmd(
        ctx,
        [
            "bash",
            "-c",
            cc,
        ],
        workdir=ctx.cli.homedir,
    )
    with ctx.user_command():
        _run(cmd, check=True, ctx=ctx)


@Builder.set(Steps.INTERACTIVE)
def bc_interactive(ctx):
    """Start an interactive shell in the build container."""
    ctx.build.wants(Steps.CONTAINER, ctx)
    cmd = _container_cmd(
        ctx,
        [],
        workdir=ctx.cli.homedir,
        interactive=True,
    )
    with ctx.user_command():
        _run(cmd, check=False, ctx=ctx)


@Builder.set(Steps.SYNC)
def bc_sync(ctx):
    """Sync files from samba-in-kubernetes/samba-build."""
    url = "https://raw.githubusercontent.com/samba-in-kubernetes/samba-build/main/packaging/samba-master.spec.j2"
    cdir = pathlib.Path(ctx.cli.containerdir)
    spec_file = cdir / "samba-master.spec"
    old_spec_file = cdir / "samba-master.spec.old"
    if spec_file.exists():
        spec_file.rename(old_spec_file)
    with tempfile.TemporaryFile() as tfh:
        # buffer the object into a local temporary file
        resp = urllib.request.urlopen(url)
        shutil.copyfileobj(resp, tfh)
        tfh.seek(0)
        # replace jinja style templating
        pattern = b"{{ samba_rpm_version }}"
        with open(spec_file, "wb") as fh:
            for line in tfh:
                if pattern in line:
                    line = line.replace(pattern, b"4.999")
                fh.write(line)


class ArgumentParser(argparse.ArgumentParser):
    def parse_my_args(self, args=None, namespace=None):
        """Parse argument up to the '--' term and then stop parsing.
        Returns a tuple of the parsed args and then remaining args.
        """
        args = sys.argv[1:] if args is None else list(args)
        if "--" in args:
            idx = args.index("--")
            my_args, rest = args[:idx], args[idx + 1 :]
        else:
            my_args, rest = args, []
        return self.parse_args(my_args, namespace=namespace), rest


def parse_cli(build_step_names):
    parser = ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Emit debugging level logging and tracebacks",
    )
    parser.add_argument(
        "--container-engine",
        help="Select container engine to use (eg. podman, docker)",
    )
    parser.add_argument(
        "--cwd",
        help="Change working directory before executing commands",
    )
    parser.add_argument(
        "--distro",
        "-d",
        choices=DistroKind.aliases().keys(),
        type=DistroKind.from_alias,
        default=str(DistroKind.CENTOS9),
        help="Specify a distro short name",
    )
    parser.add_argument(
        "--tag",
        "-t",
        help="Specify a container tag. Append to the auto generated tag"
        " by prefixing the supplied value with the plus (+) character",
    )
    parser.add_argument(
        "--base-branch",
        help="Specify a base branch name",
    )
    parser.add_argument(
        "--current-branch",
        help="Manually specify the current branch name",
    )
    parser.add_argument(
        "--image-repo",
        help="Specify a container image repository",
    )
    parser.add_argument(
        "--image-sources",
        "-I",
        type=ImageSource.argument,
        help="Specify a set of valid image sources. "
        f"May be a comma separated list of {ImageSource.hint()}",
    )
    parser.add_argument(
        "--base-image",
        help=(
            "Supply a custom base image to use instead of the default"
            " image for the source distro."
        ),
    )
    parser.add_argument(
        "--homedir",
        default="/samba",
        help="Container image home/build dir",
    )
    parser.add_argument(
        "--source-dir",
        "-s",
        type=_host_path,
        help="Path to samba git checkout",
    )
    parser.add_argument(
        "--dnf-cache-path",
        help="DNF caching using provided base dir",
    )
    parser.add_argument(
        "--build-dir",
        "-b",
        help=("Specify a build directory relative to the home dir"),
    )
    parser.add_argument(
        "--overlay-dir",
        "-l",
        help=(
            "Mount the homedir as an overlay volume using the given dir"
            "to host the overlay content and working dir. Specify '-' to"
            "use a temporary overlay (discarding writes on container exit)"
        ),
    )
    parser.add_argument(
        "--ccache-dir",
        help=(
            "Specify a directory (within the container) to save ccache"
            " output"
        ),
    )
    parser.add_argument(
        "--extra",
        "-x",
        action="append",
        help="Specify an extra argument to pass to container command",
    )
    parser.add_argument(
        "--keep-container",
        action="store_true",
        help="Skip removing container after executing command",
    )
    parser.add_argument(
        "--containerfile",
        default="Dockerfile.build",
        help="Specify the path to a (build) container file",
    )
    parser.add_argument(
        "--containerdir",
        default=".",
        help="Specify the path to container context dir",
    )
    parser.add_argument(
        "--no-prereqs",
        "-P",
        action="store_true",
        help="Do not execute any prerequisite steps. Only execute specified steps",
    )
    parser.add_argument(
        "--rpm-no-match-sha",
        dest="rpm_match_sha",
        action="store_false",
        help=(
            "Do not try to build RPM packages that match the SHA of the current"
            " git checkout. Use any source RPM available."
        ),
    )
    parser.add_argument(
        "--execute",
        "-e",
        dest="steps",
        action="append",
        choices=build_step_names,
        help="Execute the target build step(s)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Do not execute key commands, print and continue if possible",
    )
    parser.add_argument(
        "--help-build-steps",
        action="store_true",
        help="Print executable build steps and brief descriptions",
    )
    cli, rest = parser.parse_my_args()
    if cli.help_build_steps:
        print("Executable Build Steps")
        print("======================")
        print("")

        for step_name, doc in sorted(Builder().docs()):
            print(step_name)
            print(" " * 5, doc)
            print("")
        sys.exit(0)
    if rest and rest[0] == "--":
        rest[:] = rest[1:]
    cli.remaining_args = rest
    return cli


def _src_root():
    return pathlib.Path(__file__).parent.absolute()


def _host_path(value):
    return str(pathlib.Path(value).resolve())


def _short(value):
    return value[:12]


class ColorFormatter(logging.Formatter):
    _yellow = "\x1b[33;20m"
    _red = "\x1b[31;20m"
    _reset = "\x1b[0m"

    def format(self, record):
        res = super().format(record)
        if record.levelno == logging.WARNING:
            res = self._yellow + res + self._reset
        if record.levelno == logging.ERROR:
            res = self._red + res + self._reset
        return res


def _setup_logging(cli):
    level = logging.DEBUG if cli.debug else logging.INFO
    logger = logging.getLogger()
    logger.setLevel(level)
    handler = logging.StreamHandler()
    fmt = "{asctime}: {levelname}: {message}"
    if sys.stdout.isatty() and sys.stderr.isatty():
        formatter = ColorFormatter(fmt, style="{")
    else:
        formatter = logging.Formatter(fmt, style="{")
    handler.setFormatter(formatter)
    handler.setLevel(level)
    logger.addHandler(handler)


def main():
    builder = Builder()
    cli = parse_cli(builder.available_steps())
    _setup_logging(cli)

    os.chdir(cli.cwd or _src_root())
    ctx = Context(cli)
    ctx.build = builder
    try:
        for step in cli.steps or [Steps.BUILD]:
            ctx.build.wants(step, ctx, top=True)
    except CommandFailed as err:
        err_cause = getattr(err, "__cause__", None)
        if err_cause:
            log.error("Command failed: %s", err_cause)
        else:
            log.error("Command failed!")
        log.warning(
            "🚧 the command may have faild due to circumstances"
            " beyond the influence of this build script. For example: a"
            " complier error caused by a source code change."
            " Pay careful attention to the output generated by the command"
            " before reporting this as a problem with the"
            " build-with-container.py script. 🚧"
        )
        sys.exit(1)


if __name__ == "__main__":
    main()
