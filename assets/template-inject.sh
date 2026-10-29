# Adds Flint's guest files to an imported image. Runs chrooted into the
# image's root under Flint's static busybox, so every path (and any symlink the
# image ships) resolves inside the image, never in the builder.
set -e
src=/.flint-inject
bb=$src/busybox

install_file() {  # install_file SOURCE DEST MODE
    $bb rm -rf "$2"
    $bb cp "$1" "$2"
    $bb chmod "$3" "$2"
}

$bb mkdir -p /usr/local/lib/flint/bin /usr/local/bin /etc/flint
install_file $bb /usr/local/lib/flint/busybox 755
for applet in $($bb --list); do
    $bb ln -sf ../busybox "/usr/local/lib/flint/bin/$applet"
done
install_file $src/flintd /usr/local/bin/flintd 755
install_file $src/init-net.sh /etc/init-net.sh 755
install_file $src/image.env /etc/flint/image.env 644

# Commands run through /bin/sh; give shell-less images one.
if [ ! -e /bin/sh ]; then
    $bb mkdir -p /bin
    $bb rm -f /bin/sh
    $bb ln -s /usr/local/lib/flint/busybox /bin/sh
fi
