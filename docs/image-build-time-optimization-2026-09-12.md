# Image build time optimization

The runtime image was spending time on work unrelated to changed inputs:

- the source installer deleted the global builder/image cache;
- local builds injected the current timestamp into Python and frontend layers;
- the build wrapper produced a host wheel that the Containerfile ignored and
  rebuilt inside the image;
- relay packages were installed after application/ops copies, so source changes
  repeated an apt transaction;
- the public multi-architecture job emulated arm64 under QEMU in one runner.

The build now relies on content-addressed layers, preserves cache and user data
by default, builds the wheel only inside the container, installs stable relay
packages in the base dependency layer, and builds amd64/arm64 concurrently on
native GitHub runners before merging the multi-architecture index. Cache scopes
are separated per architecture. `GIT_SHA` remains late metadata and does not
invalidate the dependency/application layers before it.

`SAFENT_FACTORY_RESET=1 ./ops/container/install.sh` is the explicit path that
deletes only `safent-data`; it still never prunes unrelated global builder or
image data. A diagnostic cold build uses the container engine's `--no-cache`
option instead of silently forcing every normal build cold.

Verification is intentionally cheap before the single final image build:

- shell syntax for both wrappers;
- YAML parse of the publish workflow;
- three static regression contracts preventing timestamp cache busts, QEMU
  reintroduction, redundant wheel creation or implicit cache/data deletion.

No image was built for this optimization cut. The final release build will be
the first timing measurement; until then no invented minute saving is claimed.
