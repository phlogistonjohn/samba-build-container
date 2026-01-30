A quick and dirty containerized build wrapper for Samba.

This is meant for developers who do not want to install a changing suite of
dependencies to build Samba directly on the developer's systems and/or who want
to build packages for a different OS platform.


## Quick Start

* Check out samba: `git clone git://git.samba.org/samba.git`
* Check out this repo: `git clone https://github.com/phlogistonjohn/samba-build-container`
* Change directory: `cd samba-build-container`
* Make builds directory: `mkdir builds`
* Build code: `./build-samba.py -l ./builds/test1 -s ../samba -e build`
* Build RPM packages: `./build-samba.py -l ./builds/rpm1 -s ../samba -e rpm`

### What's going on?

This tool creates a container image (the build image) that contains what's
needed to build the source or build packages from the source. Once the
build image is created it uses the build image to execute a build command
within the container environment to compile code and/or packages.

Because this is a tool for developers the expectation is that one will use
a git clone to manage the Samba sources. This directory is provided to
the script using the `-s` option. We use the (optional) `-l` option to
create an "overlay" directory that contains all files written while
the container is running - keeping the original sources and the build
artifacts separate.

The `-e` option executes a particular build step. In the examples above
the `build` step compiles the Samba sources directly using waf. The
`rpm` step creates RPM based packages using the sources as well as
additional files (based on the samba-build project).

## Sync'ing sources

When building packages we rely on a .spec file and additional source
files that come from the samba-build project. We copy them and commit
them to this repo for convenience. Because samba master branch keeps
getting updated the `-e sync` option will automatically download
the latest version of the samba-master.spec file.

Other updates may need to be done manually.


## Development

This is a tool I developed for my own personal use, based on a similar tool
that was contributed to the Ceph project, and is being continually developed
as my needs permit. It's now mature enough that I am willing to share
it with other :-)

Please feel free to try it out and suggest features, report issues, and
consider submitting improvements.
