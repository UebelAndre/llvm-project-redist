import unittest

from tools.transform_extensions_bzl import transform

# Realistic upstream extensions.bzl (matches llvm/llvm-project main)
UPSTREAM = '''\
# This file is licensed under the Apache License v2.0 with LLVM Exceptions.
# See https://llvm.org/LICENSE.txt for license information.
# SPDX-License-Identifier: Apache-2.0 WITH LLVM-exception

"""bzlmod extensions for llvm-project"""

load("@bazel_tools//tools/build_defs/repo:http.bzl", "http_archive")
load("@bazel_tools//tools/build_defs/repo:local.bzl", "new_local_repository")
load(":vulkan_sdk.bzl", "vulkan_sdk_setup")

def _llvm_repos_extension_impl(module_ctx):
    if any([m.is_root and m.name == "llvm-project-overlay" for m in module_ctx.modules]):
        new_local_repository(
            name = "llvm-raw",
            build_file_content = "# empty",
            path = "../../",
        )

        http_archive(
            name = "vulkan_headers",
            build_file = "@llvm-raw//utils/bazel/third_party_build:vulkan_headers.BUILD",
            sha256 = "19f491784ef0bc73caff877d11c96a48b946b5a1c805079d9006e3fbaa5c1895",
            strip_prefix = "Vulkan-Headers-9bd3f561bcee3f01d22912de10bb07ce4e23d378",
            urls = [
                "https://github.com/KhronosGroup/Vulkan-Headers/archive/9bd3f561bcee3f01d22912de10bb07ce4e23d378.tar.gz",
            ],
        )

        vulkan_sdk_setup(name = "vulkan_sdk")

        http_archive(
            name = "gmp",
            urls = [
                "https://gmplib.org/download/gmp/gmp-6.2.1.tar.xz",
                "https://ftp.gnu.org/gnu/gmp/gmp-6.2.1.tar.xz",
            ],
            build_file = "@llvm-raw//utils/bazel/third_party_build:gmp.BUILD",
            sha256 = "fd4829912cddd12f84181c3451cc752be224643e87fac497b69edddadc49b4f2",
            strip_prefix = "gmp-6.2.1",
        )

        http_archive(
            name = "pfm",
            urls = [
                "https://versaweb.dl.sourceforge.net/project/perfmon2/libpfm4/libpfm-4.13.0.tar.gz",
            ],
            sha256 = "d18b97764c755528c1051d376e33545d0eb60c6ebf85680436813fa5b04cc3d1",
            strip_prefix = "libpfm-4.13.0",
            build_file = "@llvm-raw//utils/bazel/third_party_build:pfm.BUILD",
        )

llvm_repos_extension = module_extension(
    implementation = _llvm_repos_extension_impl,
)
'''


class TransformExtensionsBzlTest(unittest.TestCase):
    def setUp(self):
        self.result = transform(UPSTREAM)

    # ---- new_local_repository for llvm-raw removed ----

    def test_new_local_repository_removed(self):
        self.assertNotIn("new_local_repository(", self.result)

    def test_llvm_raw_name_removed(self):
        self.assertNotIn('"llvm-raw"', self.result)

    # ---- local.bzl import removed ----

    def test_local_bzl_import_removed(self):
        self.assertNotIn("local.bzl", self.result)

    def test_http_archive_import_preserved(self):
        self.assertIn(
            'load("@bazel_tools//tools/build_defs/repo:http.bzl", "http_archive")',
            self.result,
        )

    def test_vulkan_sdk_import_preserved(self):
        self.assertIn(
            'load(":vulkan_sdk.bzl", "vulkan_sdk_setup")',
            self.result,
        )

    # ---- @llvm-raw labels replaced with Label() ----

    def test_at_llvm_raw_labels_removed(self):
        self.assertNotIn("@llvm-raw//", self.result)

    def test_labels_rewritten_to_label_call(self):
        self.assertIn(
            'Label("//utils/bazel/third_party_build:vulkan_headers.BUILD")',
            self.result,
        )
        self.assertIn(
            'Label("//utils/bazel/third_party_build:gmp.BUILD")',
            self.result,
        )
        self.assertIn(
            'Label("//utils/bazel/third_party_build:pfm.BUILD")',
            self.result,
        )

    # ---- module name guard updated ----

    def test_module_name_updated(self):
        self.assertIn('"llvm-project"', self.result)
        self.assertNotIn('"llvm-project-overlay"', self.result)

    # ---- http_archive blocks preserved ----

    def test_http_archive_blocks_intact(self):
        self.assertIn('name = "vulkan_headers"', self.result)
        self.assertIn('name = "gmp"', self.result)
        self.assertIn('name = "pfm"', self.result)

    def test_sha256_preserved(self):
        self.assertIn(
            "19f491784ef0bc73caff877d11c96a48b946b5a1c805079d9006e3fbaa5c1895",
            self.result,
        )

    def test_urls_preserved(self):
        self.assertIn("https://gmplib.org/download/gmp/gmp-6.2.1.tar.xz", self.result)

    def test_vulkan_sdk_setup_preserved(self):
        self.assertIn('vulkan_sdk_setup(name = "vulkan_sdk")', self.result)

    # ---- module_extension preserved ----

    def test_module_extension_preserved(self):
        self.assertIn("llvm_repos_extension = module_extension(", self.result)

    # ---- structural ----

    def test_header_comments_preserved(self):
        self.assertIn("Apache License v2.0 with LLVM Exceptions", self.result)

    def test_no_triple_blank_lines(self):
        self.assertNotIn("\n\n\n", self.result)

    # ---- empty input ----

    def test_empty_input(self):
        result = transform("")
        self.assertEqual(result, "")


if __name__ == "__main__":
    unittest.main()
