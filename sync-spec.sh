#!/bin/bash
# Synchronize the samba master spec file with the onse from samba-build
# project.

set -e

url="https://raw.githubusercontent.com/samba-in-kubernetes/samba-build/main/packaging/samba-master.spec.j2"

curl -q -O "$url"
sed 's/{{ samba_rpm_version }}/4.999/g' < samba-master.spec.j2 > samba-master.spec.new
rm -f samba-master.spec.j2

echo "Created: samba-master.spec.new"
echo "Compare:  diff -u samba-master.spec  samba-master.spec.new"
