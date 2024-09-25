# Copyright 2013-2024 Lawrence Livermore National Security, LLC and other
# Spack Project Developers. See the top-level COPYRIGHT file for details.
#
# SPDX-License-Identifier: (Apache-2.0 OR MIT)
"""Test basic behavior of compilers in Spack"""
import os
from copy import copy

import pytest

import llnl.util.filesystem as fs

import spack.compilers.config
import spack.compilers.error
import spack.config
import spack.spec
import spack.util.module_cmd
from spack.compiler import Compiler
from spack.util.executable import Executable

# FIXME (compiler as nodes): revisit this test
# def test_multiple_conflicting_compiler_definitions(mutable_config):
#     compiler_def = {
#         "compiler": {
#             "flags": {},
#             "modules": [],
#             "paths": {"cc": "cc", "cxx": "cxx", "f77": "null", "fc": "null"},
#             "extra_rpaths": [],
#             "operating_system": "test",
#             "target": "test",
#             "environment": {},
#             "spec": "clang@0.0.0",
#         }
#     }
#
#     compiler_config = [compiler_def, compiler_def]
#     compiler_config[0]["compiler"]["paths"]["f77"] = "f77"
#     mutable_config.update_config("compilers", compiler_config)
#
#     arch_spec = spack.spec.ArchSpec(("test", "test", "test"))
#     cmp = spack.compilers.config.compiler_for_spec("clang@=0.0.0", arch_spec)
#     assert cmp.f77 == "f77"


def test_compiler_flags_from_config_are_grouped():
    compiler_entry = {
        "spec": "intel@17.0.2",
        "operating_system": "foo-os",
        "paths": {"cc": "cc-path", "cxx": "cxx-path", "fc": None, "f77": None},
        "flags": {"cflags": "-O0 -foo-flag foo-val"},
        "modules": None,
    }

    compiler = spack.compilers.config.compiler_from_dict(compiler_entry)
    assert any(x == "-foo-flag foo-val" for x in compiler.flags["cflags"])


# Test behavior of flags and UnsupportedCompilerFlag.

# Utility function to test most flags.
default_compiler_entry = {
    "spec": "apple-clang@2.0.0",
    "operating_system": "foo-os",
    "paths": {"cc": "cc-path", "cxx": "cxx-path", "fc": "fc-path", "f77": "f77-path"},
    "flags": {},
    "modules": None,
}


# Fake up a mock compiler where everything is defaulted.
class MockCompiler(Compiler):
    def __init__(self):
        super().__init__(
            cspec="badcompiler@1.0.0",
            operating_system=default_compiler_entry["operating_system"],
            target=None,
            paths=[
                default_compiler_entry["paths"]["cc"],
                default_compiler_entry["paths"]["cxx"],
                default_compiler_entry["paths"]["fc"],
                default_compiler_entry["paths"]["f77"],
            ],
            environment={},
        )

    @property
    def name(self):
        return "mockcompiler"

    @property
    def version(self):
        return "1.0.0"

    _verbose_flag = "--verbose"

    @property
    def verbose_flag(self):
        return self._verbose_flag

    required_libs = ["libgfortran"]


@pytest.mark.not_on_windows("Not supported on Windows (yet)")
def test_implicit_rpaths(dirs_with_libfiles):
    lib_to_dirs, all_dirs = dirs_with_libfiles
    compiler = MockCompiler()
    compiler._compile_c_source_output = "ld " + " ".join(f"-L{d}" for d in all_dirs)
    retrieved_rpaths = compiler.implicit_rpaths()
    assert set(retrieved_rpaths) == set(lib_to_dirs["libstdc++"] + lib_to_dirs["libgfortran"])


without_flag_output = "ld -L/path/to/first/lib -L/path/to/second/lib64"
with_flag_output = "ld -L/path/to/first/with/flag/lib -L/path/to/second/lib64"


def call_compiler(exe, *args, **kwargs):
    # This method can replace Executable.__call__ to emulate a compiler that
    # changes libraries depending on a flag.
    if "--correct-flag" in exe.exe:
        return with_flag_output
    return without_flag_output


@pytest.mark.not_on_windows("Not supported on Windows (yet)")
@pytest.mark.parametrize(
    "exe,flagname",
    [
        ("cxx", "cxxflags"),
        ("cxx", "cppflags"),
        ("cxx", "ldflags"),
        ("cc", "cflags"),
        ("cc", "cppflags"),
    ],
)
@pytest.mark.enable_compiler_execution
def test_compile_dummy_c_source_adds_flags(monkeypatch, exe, flagname):
    # create fake compiler that emits mock verbose output
    compiler = MockCompiler()
    monkeypatch.setattr(Executable, "__call__", call_compiler)

    if exe == "cxx":
        compiler.cc = None
        compiler.fc = None
        compiler.f77 = None
    elif exe == "cc":
        compiler.cxx = None
        compiler.fc = None
        compiler.f77 = None
    else:
        assert False

    # Test without flags
    assert compiler._compile_dummy_c_source() == without_flag_output

    if flagname:
        # set flags and test
        compiler.flags = {flagname: ["--correct-flag"]}
        assert compiler._compile_dummy_c_source() == with_flag_output


@pytest.mark.enable_compiler_execution
def test_compile_dummy_c_source_no_path():
    compiler = MockCompiler()
    compiler.cc = None
    compiler.cxx = None
    assert compiler._compile_dummy_c_source() is None


@pytest.mark.enable_compiler_execution
def test_compile_dummy_c_source_no_verbose_flag():
    compiler = MockCompiler()
    compiler._verbose_flag = None
    assert compiler._compile_dummy_c_source() is None


@pytest.mark.not_on_windows("Not supported on Windows (yet)")
@pytest.mark.enable_compiler_execution
def test_compile_dummy_c_source_load_env(working_env, monkeypatch, tmpdir):
    gcc = str(tmpdir.join("gcc"))
    with open(gcc, "w") as f:
        f.write(
            f"""#!/bin/sh
if [ "$ENV_SET" = "1" ] && [ "$MODULE_LOADED" = "1" ]; then
  printf '{without_flag_output}'
fi
"""
        )
    fs.set_executable(gcc)

    # Set module load to turn compiler on
    def module(*args):
        if args[0] == "show":
            return ""
        elif args[0] == "load":
            os.environ["MODULE_LOADED"] = "1"

    monkeypatch.setattr(spack.util.module_cmd, "module", module)

    compiler = MockCompiler()
    compiler.cc = gcc
    compiler.environment = {"set": {"ENV_SET": "1"}}
    compiler.modules = ["turn_on"]

    assert compiler._compile_dummy_c_source() == without_flag_output


# Get the desired flag from the specified compiler spec.
def flag_value(flag, spec):
    compiler = None
    if spec is None:
        compiler = MockCompiler()
    else:
        compiler_entry = copy(default_compiler_entry)
        compiler_entry["spec"] = spec
        compiler = spack.compilers.config.compiler_from_dict(compiler_entry)

    return getattr(compiler, flag)


# Utility function to verify that the expected exception is thrown for
# an unsupported flag.
def unsupported_flag_test(flag, spec=None):
    caught_exception = None
    try:
        flag_value(flag, spec)
    except spack.compilers.error.UnsupportedCompilerFlag:
        caught_exception = True

    assert caught_exception and "Expected exception not thrown."


# Verify the expected flag value for the give compiler spec.
def supported_flag_test(flag, flag_value_ref, spec=None):
    assert flag_value(flag, spec) == flag_value_ref


# FIXME (compiler as nodes): revisit this test
# @pytest.mark.regression("14798,13733")
# def test_raising_if_compiler_target_is_over_specific(config):
#     # Compiler entry with an overly specific target
#     compilers = [
#         {
#             "compiler": {
#                 "spec": "gcc@9.0.1",
#                 "paths": {
#                     "cc": "/usr/bin/gcc-9",
#                     "cxx": "/usr/bin/g++-9",
#                     "f77": "/usr/bin/gfortran-9",
#                     "fc": "/usr/bin/gfortran-9",
#                 },
#                 "flags": {},
#                 "operating_system": "ubuntu18.04",
#                 "target": "haswell",
#                 "modules": [],
#                 "environment": {},
#                 "extra_rpaths": [],
#             }
#         }
#     ]
#     arch_spec = spack.spec.ArchSpec(("linux", "ubuntu18.04", "haswell"))
#     with spack.config.override("compilers", compilers):
#         cfg = spack.compilers.config.get_compiler_config(config)
#         with pytest.raises(ValueError):
#             spack.compilers.config.get_compilers(
#                 cfg, spack.spec.CompilerSpec("gcc@9.0.1"), arch_spec
#             )

# FIXME (compiler as nodes): revisit this test
# @pytest.mark.regression("42679")
# def test_get_compilers(config):
#     """Tests that we can select compilers whose versions differ only for a suffix."""
#     common = {
#         "flags": {},
#         "operating_system": "ubuntu23.10",
#         "target": "x86_64",
#         "modules": [],
#         "environment": {},
#         "extra_rpaths": [],
#     }
#     with_suffix = {
#         "spec": "gcc@13.2.0-suffix",
#         "paths": {
#             "cc": "/usr/bin/gcc-13.2.0-suffix",
#             "cxx": "/usr/bin/g++-13.2.0-suffix",
#             "f77": "/usr/bin/gfortran-13.2.0-suffix",
#             "fc": "/usr/bin/gfortran-13.2.0-suffix",
#         },
#         **common,
#     }
#     without_suffix = {
#         "spec": "gcc@13.2.0",
#         "paths": {
#             "cc": "/usr/bin/gcc-13.2.0",
#             "cxx": "/usr/bin/g++-13.2.0",
#             "f77": "/usr/bin/gfortran-13.2.0",
#             "fc": "/usr/bin/gfortran-13.2.0",
#         },
#         **common,
#     }
#
#     compilers = [{"compiler": without_suffix}, {"compiler": with_suffix}]
#
#     assert spack.compilers.config.get_compilers(
#         compilers, cspec=spack.spec.CompilerSpec("gcc@=13.2.0-suffix")
#     ) == [spack.compilers.config._compiler_from_config_entry(with_suffix)]
#
#     assert spack.compilers.config.get_compilers(
#         compilers, cspec=spack.spec.CompilerSpec("gcc@=13.2.0")
#     ) == [spack.compilers.config._compiler_from_config_entry(without_suffix)]


@pytest.mark.enable_compiler_verification
def test_compiler_executable_verification_raises(tmpdir):
    compiler = MockCompiler()
    compiler.cc = "/this/path/does/not/exist"

    with pytest.raises(spack.compilers.error.CompilerAccessError):
        compiler.verify_executables()


@pytest.mark.enable_compiler_verification
def test_compiler_executable_verification_success(tmpdir):
    def prepare_executable(name):
        real = str(tmpdir.join("cc").ensure())
        fs.set_executable(real)
        setattr(compiler, name, real)

    # setup mock compiler with real paths
    compiler = MockCompiler()
    for name in ("cc", "cxx", "f77", "fc"):
        prepare_executable(name)

    # testing that this doesn't raise an error because the paths exist and
    # are executable
    compiler.verify_executables()

    # Test that null entries don't fail
    compiler.cc = None
    compiler.verify_executables()


@pytest.mark.parametrize(
    "compilers_extra_attributes,expected_length",
    [
        # If we detect a C compiler we expect the result to be valid
        ({"c": "/usr/bin/clang-12", "cxx": "/usr/bin/clang-12"}, 1),
        # If we detect only a C++ compiler we expect the result to be discarded
        ({"cxx": "/usr/bin/clang-12"}, 0),
    ],
)
def test_detection_requires_c_compiler(compilers_extra_attributes, expected_length):
    """Tests that compilers automatically added to the configuration have
    at least a C compiler.
    """
    packages_yaml = {
        "llvm": {
            "externals": [
                {
                    "spec": "clang@12.0.0",
                    "prefix": "/usr",
                    "extra_attributes": {"compilers": compilers_extra_attributes},
                }
            ]
        }
    }
    result = spack.compilers.config.CompilerFactory.from_packages_yaml(packages_yaml)
    assert len(result) == expected_length


def test_compiler_environment(working_env):
    """Test whether environment modifications from compilers are applied in compiler_environment"""
    os.environ.pop("TEST", None)
    compiler = Compiler(
        "gcc@=13.2.0",
        operating_system="ubuntu20.04",
        target="x86_64",
        paths=["/test/bin/gcc", "/test/bin/g++"],
        environment={"set": {"TEST": "yes"}},
    )
    with compiler.compiler_environment():
        assert os.environ["TEST"] == "yes"
