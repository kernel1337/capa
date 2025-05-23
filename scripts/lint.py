# Copyright 2020 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""
Check the given capa rules for style issues.

Usage:

   $ python scripts/lint.py rules/
"""

import gc
import os
import re
import sys
import json
import time
import string
import difflib
import hashlib
import logging
import argparse
import itertools
import posixpath
from typing import Any, Dict, List
from pathlib import Path
from dataclasses import field, dataclass

import pydantic
import ruamel.yaml
from rich import print

import capa.main
import capa.rules
import capa.engine
import capa.loader
import capa.helpers
import capa.features.insn
import capa.capabilities.common
from capa.rules import Rule, RuleSet
from capa.features.common import OS_AUTO, Regex, String, Feature, Substring
from capa.render.result_document import RuleMetadata

logger = logging.getLogger("lint")


@dataclass
class Context:
    """
    attributes:
      samples: mapping from content hash (MD5, SHA, etc.) to file path.
      rules: rules to inspect
      is_thorough: should inspect long-running lints
      capabilities_by_sample: cache of results, indexed by file path.
    """

    samples: dict[str, Path]
    rules: RuleSet
    is_thorough: bool
    capabilities_by_sample: dict[Path, set[str]] = field(default_factory=dict)


class Lint:
    WARN = "[yellow]WARN[/yellow]"
    FAIL = "[red]FAIL[/red]"

    name: str = "lint"
    level: str = FAIL
    recommendation: str = ""

    def check_rule(self, ctx: Context, rule: Rule) -> bool:
        return False


class NameCasing(Lint):
    name = "rule name casing"
    recommendation = "Rename rule using to start with lower case letters"

    def check_rule(self, ctx: Context, rule: Rule):
        return rule.name[0] in string.ascii_uppercase and rule.name[1] not in string.ascii_uppercase


class FilenameDoesntMatchRuleName(Lint):
    name = "filename doesn't match the rule name"
    recommendation = "Rename rule file to match the rule name"
    recommendation_template = 'Rename rule file to match the rule name, expected: "{:s}", found: "{:s}"'

    def check_rule(self, ctx: Context, rule: Rule):
        expected = rule.name
        expected = expected.lower()
        expected = expected.replace(".net", "dotnet")
        expected = expected.replace(" ", "-")
        expected = expected.replace("(", "")
        expected = expected.replace(")", "")
        expected = expected.replace("+", "")
        expected = expected.replace("/", "")
        expected = expected.replace(".", "")
        expected = expected + ".yml"

        found = Path(rule.meta["capa/path"]).name

        self.recommendation = self.recommendation_template.format(expected, found)

        return expected != found


class MissingNamespace(Lint):
    name = "missing rule namespace"
    recommendation = "Add meta.namespace so that the rule is emitted correctly"

    def check_rule(self, ctx: Context, rule: Rule):
        return (
            "namespace" not in rule.meta
            and not is_nursery_rule(rule)
            and "maec/malware-category" not in rule.meta
            and "lib" not in rule.meta
        )


class NamespaceDoesntMatchRulePath(Lint):
    name = "file path doesn't match rule namespace"
    recommendation = "Move rule to appropriate directory or update the namespace"

    def check_rule(self, ctx: Context, rule: Rule):
        # let the other lints catch namespace issues
        if "namespace" not in rule.meta:
            return False
        if is_nursery_rule(rule):
            return False
        if "maec/malware-category" in rule.meta:
            return False
        if "lib" in rule.meta:
            return False

        return rule.meta["namespace"] not in get_normpath(rule.meta["capa/path"])


class MissingScopes(Lint):
    name = "missing scopes"
    recommendation = (
        "Add meta.scopes with both the static (meta.scopes.static) and dynamic (meta.scopes.dynamic) scopes"
    )

    def check_rule(self, ctx: Context, rule: Rule):
        return "scopes" not in rule.meta


class MissingStaticScope(Lint):
    name = "missing static scope"
    recommendation = "Add a static scope for the rule (file, function, basic block, instruction, or unsupported)"

    def check_rule(self, ctx: Context, rule: Rule):
        return "static" not in rule.meta.get("scopes")


class MissingDynamicScope(Lint):
    name = "missing dynamic scope"
    recommendation = "Add a dynamic scope for the rule (file, process, thread, call, or unsupported)"

    def check_rule(self, ctx: Context, rule: Rule):
        return "dynamic" not in rule.meta.get("scopes")


class InvalidStaticScope(Lint):
    name = "invalid static scope"
    recommendation = "For the static scope, use either: file, function, basic block, instruction, or unsupported"

    def check_rule(self, ctx: Context, rule: Rule):
        return rule.meta.get("scopes").get("static") not in (
            "file",
            "function",
            "basic block",
            "instruction",
            "unsupported",
        )


class InvalidDynamicScope(Lint):
    name = "invalid static scope"
    recommendation = "For the dynamic scope, use either: file, process, thread, call, or unsupported"

    def check_rule(self, ctx: Context, rule: Rule):
        return rule.meta.get("scopes").get("dynamic") not in (
            "file",
            "process",
            "thread",
            "span of calls",
            "call",
            "unsupported",
        )


class InvalidScopes(Lint):
    name = "invalid scopes"
    recommendation = "At least one scope (static or dynamic) must be specified"

    def check_rule(self, ctx: Context, rule: Rule):
        return (rule.meta.get("scopes").get("static") == "unsupported") and (
            rule.meta.get("scopes").get("dynamic") == "unsupported"
        )


class MissingAuthors(Lint):
    name = "missing authors"
    recommendation = "Add meta.authors so that users know who to contact with questions"

    def check_rule(self, ctx: Context, rule: Rule):
        return "authors" not in rule.meta


class MissingExamples(Lint):
    name = "missing examples"
    recommendation = "Add meta.examples so that the rule can be tested and verified"

    def check_rule(self, ctx: Context, rule: Rule):
        return (
            "examples" not in rule.meta
            or not isinstance(rule.meta["examples"], list)
            or len(rule.meta["examples"]) == 0
            or rule.meta["examples"] == [None]
        )


class MissingExampleOffset(Lint):
    name = "missing example offset"
    recommendation = "Add offset of example function"

    def check_rule(self, ctx: Context, rule: Rule):
        if rule.meta.get("scope") in ("function", "basic block"):
            examples = rule.meta.get("examples")
            if isinstance(examples, list):
                for example in examples:
                    if example and ":" not in example:
                        logger.debug("example: %s", example)
                        return True


class ExampleFileDNE(Lint):
    name = "referenced example doesn't exist"
    recommendation = "Add the referenced example to samples directory ($capa-root/tests/data or supplied via --samples)"

    def check_rule(self, ctx: Context, rule: Rule):
        if not rule.meta.get("examples"):
            # let the MissingExamples lint catch this case, don't double report.
            return False

        found = False
        for example in rule.meta.get("examples", []):
            if example:
                example_id = example.partition(":")[0]
                if example_id in ctx.samples:
                    found = True
                    break

        return not found


class IncorrectValueType(Lint):
    name = "incorrect value type"
    recommendation = "Change value type"

    def check_rule(self, ctx: Context, rule: Rule):
        try:
            _ = RuleMetadata.from_capa(rule)
        except pydantic.ValidationError as e:
            self.recommendation = str(e).strip()
            return True
        return False


class InvalidAttckOrMbcTechnique(Lint):
    name = "att&ck/mbc entry is malformed or does not exist"
    recommendation = """
    The att&ck and mbc fields must respect the following format:
    <Tactic/Objective>::<Technique/Behavior> [<ID>]
    OR
    <Tactic/Objective>::<Technique/Behavior>::<Subtechnique/Method> [<ID.SubID>]
    """

    def __init__(self):
        super().__init__()

        try:
            data_path = Path(__file__).resolve().parent / "linter-data.json"
            with data_path.open("rb") as fd:
                self.data = json.load(fd)
            self.enabled_frameworks = self.data.keys()
        except (FileNotFoundError, json.decoder.JSONDecodeError):
            # linter-data.json missing, or JSON error: log an error and skip this lint
            logger.warning(
                "Could not load 'scripts/linter-data.json'. The att&ck and mbc information will not be linted."
            )
            self.enabled_frameworks = []

        # This regex matches the format defined in the recommendation attribute
        self.reg = re.compile(r"^([\w\s-]+)::(.+) \[([A-Za-z0-9.]+)\]$")

    def _entry_check(self, framework, category, entry, eid):
        if category not in self.data[framework].keys():
            self.recommendation = f'Unknown category: "{category}"'
            return True
        if eid not in self.data[framework][category].keys():
            self.recommendation = f"Unknown entry ID: {eid}"
            return True
        if self.data[framework][category][eid] != entry:
            self.recommendation = (
                f'{eid} should be associated to entry "{self.data[framework][category][eid]}" instead of "{entry}"'
            )
            return True
        return False

    def check_rule(self, ctx: Context, rule: Rule):
        for framework in self.enabled_frameworks:
            if framework in rule.meta:
                for r in rule.meta[framework]:
                    m = self.reg.match(r)
                    if m is None:
                        return True

                    args = m.group(1, 2, 3)
                    if self._entry_check(framework, *args):
                        return True
        return False


DEFAULT_SIGNATURES = capa.main.get_default_signatures()


def get_sample_capabilities(ctx: Context, path: Path) -> set[str]:
    nice_path = path.resolve().absolute()
    if path in ctx.capabilities_by_sample:
        logger.debug("found cached results: %s: %d capabilities", nice_path, len(ctx.capabilities_by_sample[path]))
        return ctx.capabilities_by_sample[path]

    logger.debug("analyzing sample: %s", nice_path)

    args = argparse.Namespace(input_file=nice_path, format=capa.main.FORMAT_AUTO, backend=capa.main.BACKEND_AUTO)
    format_ = capa.main.get_input_format_from_cli(args)
    backend = capa.main.get_backend_from_cli(args, format_)

    extractor = capa.loader.get_extractor(
        nice_path,
        format_,
        OS_AUTO,
        backend,
        DEFAULT_SIGNATURES,
        should_save_workspace=False,
        disable_progress=True,
    )

    capabilities = capa.capabilities.common.find_capabilities(ctx.rules, extractor, disable_progress=True)
    # mypy doesn't seem to be happy with the MatchResults type alias & set(...keys())?
    # so we ignore a few types here.
    capabilities = set(capabilities.matches.keys())  # type: ignore
    assert isinstance(capabilities, set)

    logger.debug("computed results: %s: %d capabilities", nice_path, len(capabilities))
    ctx.capabilities_by_sample[path] = capabilities

    # when i (wb) run the linter in thorough mode locally,
    # the OS occasionally kills the process due to memory usage.
    # so, be extra aggressive in keeping memory usage down.
    #
    # tbh, im not sure this actually does anything, but maybe it helps?
    gc.collect()

    return capabilities


class DoesntMatchExample(Lint):
    name = "doesn't match on referenced example"
    recommendation = "Fix the rule logic or provide a different example"

    def check_rule(self, ctx: Context, rule: Rule):
        if not ctx.is_thorough:
            return False

        examples = rule.meta.get("examples", [])
        if not examples:
            return False

        for example in examples:
            example_id = example.partition(":")[0]
            try:
                path = ctx.samples[example_id]
            except KeyError:
                # lint ExampleFileDNE will catch this.
                # don't double report.
                continue

            try:
                capabilities = get_sample_capabilities(ctx, path)
            except Exception as e:
                logger.exception("failed to extract capabilities: %s %s %s", rule.name, path, e)
                return True

            if rule.name not in capabilities:
                logger.info('rule "%s" does not match for sample %s', rule.name, example_id)
                return True


class StatementWithSingleChildStatement(Lint):
    name = "rule contains one or more statements with a single child statement"
    recommendation = "remove the superfluous parent statement"
    recommendation_template = "remove the superfluous parent statement: {:s}"
    violation = False

    def check_rule(self, ctx: Context, rule: Rule):
        self.violation = False

        def rec(statement, is_root=False):
            if isinstance(statement, (capa.engine.And, capa.engine.Or)):
                children = list(statement.get_children())
                if not is_root and len(children) == 1 and isinstance(children[0], capa.engine.Statement):
                    self.recommendation = self.recommendation_template.format(str(statement))
                    self.violation = True
                for child in children:
                    rec(child)

        rec(rule.statement, is_root=True)

        return self.violation


class OrStatementWithAlwaysTrueChild(Lint):
    name = "rule contains an `or` statement that's always True because of an `optional` or other child statement that's always True"
    recommendation = "clarify the rule logic, e.g. by moving the always True child statement"
    recommendation_template = "clarify the rule logic, e.g. by moving the always True child statement: {:s}"
    violation = False

    def check_rule(self, ctx: Context, rule: Rule):
        self.violation = False

        def rec(statement):
            if isinstance(statement, capa.engine.Or):
                children = list(statement.get_children())
                for child in children:
                    # `Some` implements `optional` which is an alias for `0 or more`
                    if isinstance(child, capa.engine.Some) and child.count == 0:
                        self.recommendation = self.recommendation_template.format(str(child))
                        self.violation = True
                    rec(child)

        rec(rule.statement)

        return self.violation


class NotNotUnderAnd(Lint):
    name = "rule contains a `not` statement that's not found under an `and` statement"
    recommendation = "clarify the rule logic and ensure `not` is always found under `and`"
    violation = False

    def check_rule(self, ctx: Context, rule: Rule):
        self.violation = False

        def rec(statement):
            if isinstance(statement, capa.engine.Statement):
                if not isinstance(statement, capa.engine.And):
                    for child in statement.get_children():
                        if isinstance(child, capa.engine.Not):
                            self.violation = True

                for child in statement.get_children():
                    rec(child)

        rec(rule.statement)

        return self.violation


class RuleDependencyScopeMismatch(Lint):
    name = "rule dependency scope mismatch"
    level = Lint.FAIL
    recommendation_template: str = "rule '{:s}' ({:s}) depends on rule '{:s}' ({:s})."

    def check_rule(self, ctx: Context, rule: Rule):
        # get all rules by name for quick lookup
        rules_by_name = {r.name: r for r in ctx.rules.rules.values()}

        # get all dependencies of this rule
        namespaces = ctx.rules.rules_by_namespace
        dependencies = rule.get_dependencies(namespaces)

        for dep_name in dependencies:
            if dep_name not in rules_by_name:
                # another lint will catch missing dependencies
                continue

            dep_rule = rules_by_name[dep_name]

            if rule.scopes.static and not self._is_static_scope_compatible(rule, dep_rule):
                self.recommendation = self.recommendation_template.format(
                    rule.name,
                    rule.scopes.static or "static: unsupported",
                    dep_name,
                    dep_rule.scopes.static or "static: unsupported",
                )
                return True

            if rule.scopes.dynamic and not self._is_dynamic_scope_compatible(rule, dep_rule):
                self.recommendation = self.recommendation_template.format(
                    rule.name,
                    rule.scopes.dynamic or "dynamic: unsupported",
                    dep_name,
                    dep_rule.scopes.dynamic or "dynamic: unsupported",
                )
                return True

        return False

    @staticmethod
    def _is_static_scope_compatible(parent: Rule, child: Rule) -> bool:
        """
        A child rule's scope is compatible if it is equal to or lower than the parent scope.
        """

        if parent.scopes.static and not child.scopes.static and child.is_subscope_rule():
            # this is ok: the child isn't a static subscope rule
            return True

        if parent.scopes.static and not child.scopes.static:
            # This is not really ok, but we can't really be sure here:
            #  the parent is a static rule, and the child is not,
            #  and we don't know if this is strictly required to match.
            # Assume for now it is not.
            return True

        assert child.scopes.static is not None
        return capa.rules.is_subscope_compatible(parent.scopes.static, child.scopes.static)

    @staticmethod
    def _is_dynamic_scope_compatible(parent: Rule, child: Rule) -> bool:
        """
        A child rule's scope is compatible if it is equal to or lower than the parent scope.
        """

        if parent.scopes.dynamic and not child.scopes.dynamic and child.is_subscope_rule():
            # this is ok: the child isn't a dynamic subscope rule
            return True

        if parent.scopes.dynamic and not child.scopes.dynamic:
            # This is not really ok, but we can't really be sure here:
            #  the parent is a dynamic rule, and the child is not,
            #  and we don't know if this is strictly required to match.
            # Assume for now it is not.
            return True

        assert child.scopes.dynamic is not None
        return capa.rules.is_subscope_compatible(parent.scopes.dynamic, child.scopes.dynamic)


class OptionalNotUnderAnd(Lint):
    name = "rule contains an `optional` or `0 or more` statement that's not found under an `and` statement"
    recommendation = "clarify the rule logic and ensure `optional` and `0 or more` is always found under `and`"
    violation = False

    def check_rule(self, ctx: Context, rule: Rule):
        self.violation = False

        def rec(statement):
            if isinstance(statement, capa.engine.Statement):
                if not isinstance(statement, capa.engine.And):
                    for child in statement.get_children():
                        if isinstance(child, capa.engine.Some) and child.count == 0:
                            self.violation = True

                for child in statement.get_children():
                    rec(child)

        rec(rule.statement)

        return self.violation


class DuplicateFeatureUnderStatement(Lint):
    name = "rule contains a duplicate features"
    recommendation = "remove the duplicate features"
    recommendation_template = '\n\tduplicate line: "{:s}"\t: line numbers: {:s}'
    violation = False

    def check_rule(self, ctx: Context, rule: Rule) -> bool:
        self.violation = False
        self.recommendation = ""
        STATEMENTS = frozenset(
            {
                "or",
                "and",
                "not",
                "optional",
                "some",
                "basic block",
                "function",
                "instruction",
                "call",
                " or more",
            }
        )
        # rule.statement discards the duplicate features by default so
        # need to use the rule definition to check for duplicates
        data = rule._get_ruamel_yaml_parser().load(rule.definition)

        def get_line_number(line: Dict[str, Any]) -> int:
            lc = getattr(line, "lc", None)
            if lc and hasattr(lc, "line"):
                return lc.line + 1
            return 0

        def is_statement(key: str) -> bool:
            # to generalize the check for 'n or more' statements
            return any(statement in key for statement in STATEMENTS)

        def get_feature_key(feature_dict: Dict[str, Any]) -> str:
            # need this for generating key for multi-lined feature
            # for example,         - string: /dbghelp\.dll/i
            #                        description: WindBG
            parts = []
            for key, value in list(feature_dict.items()):
                parts.append(f"{key}: {value}")
            return "- " + ", ".join(parts)

        def find_duplicates(features: List[Any]) -> None:
            if not isinstance(features, list):
                return

            seen_features: Dict[str, List[int]] = {}
            for item in features:
                if not isinstance(item, dict):
                    continue

                if any(is_statement(key) for key in item.keys()):
                    for key, value in item.items():
                        if is_statement(key):
                            # recursively check nested features
                            find_duplicates(value)
                    continue

                feature_key = get_feature_key(item)
                line_num = get_line_number(item)
                if feature_key in seen_features:
                    self.violation = True
                    seen_features[feature_key].append(line_num)
                else:
                    seen_features[feature_key] = [line_num]
            for feature_key, line_numbers in seen_features.items():
                if len(line_numbers) > 1:
                    sorted_lines = sorted(line_numbers)
                    self.recommendation += self.recommendation_template.format(
                        feature_key, ", ".join(str(line) for line in sorted_lines)
                    )

        features = data["rule"].get("features", [])
        find_duplicates(features)

        return self.violation


class UnusualMetaField(Lint):
    name = "unusual meta field"
    recommendation = "Remove the meta field"
    recommendation_template = 'Remove the meta field: "{:s}"'

    def check_rule(self, ctx: Context, rule: Rule):
        for key in rule.meta.keys():
            if key in capa.rules.META_KEYS:
                continue
            if key in capa.rules.HIDDEN_META_KEYS:
                continue
            self.recommendation = self.recommendation_template.format(key)
            return True

        return False


class LibRuleNotInLibDirectory(Lint):
    name = "lib rule not found in lib directory"
    recommendation = "Move the rule to the `lib` subdirectory of the rules path"

    def check_rule(self, ctx: Context, rule: Rule):
        if is_nursery_rule(rule):
            return False

        if "lib" not in rule.meta:
            return False

        return "lib/" not in get_normpath(rule.meta["capa/path"])


class LibRuleHasNamespace(Lint):
    name = "lib rule has a namespace"
    recommendation = "Remove the namespace from the rule"

    def check_rule(self, ctx: Context, rule: Rule):
        if "lib" not in rule.meta:
            return False

        return "namespace" in rule.meta


class FeatureStringTooShort(Lint):
    name = "feature string too short"
    recommendation = 'capa only extracts strings with length >= 4; will not match on "{:s}"'

    def check_features(self, ctx: Context, features: list[Feature]):
        for feature in features:
            if isinstance(feature, (String, Substring)):
                assert isinstance(feature.value, str)
                if len(feature.value) < 4:
                    self.recommendation = self.recommendation.format(feature.value)
                    return True
        return False


class FeatureRegexRegistryControlSetMatchIncomplete(Lint):
    name = "feature regex registry control set match incomplete"
    recommendation = (
        'use "(ControlSet\\d{3}|CurrentControlSet)" to match both indirect references '
        + 'via "CurrentControlSet" and direct references via "ControlSetXXX"'
    )

    def check_features(self, ctx: Context, features: list[Feature]):
        for feature in features:
            if not isinstance(feature, (Regex,)):
                continue

            assert isinstance(feature.value, str)

            pat = feature.value.lower()

            if "system\\\\" in pat and "controlset" in pat or "currentcontrolset" in pat:
                if "system\\\\(controlset\\d{3}|currentcontrolset)" not in pat:
                    return True

            return False


class FeatureRegexContainsUnescapedPeriod(Lint):
    name = "feature regex contains unescaped period"
    recommendation_template = 'escape the period in "{:s}" unless it should be treated as a regex dot operator'
    level = Lint.WARN

    def check_features(self, ctx: Context, features: list[Feature]):
        for feature in features:
            if isinstance(feature, (Regex,)):
                assert isinstance(feature.value, str)

                pat = feature.value.removeprefix("/")
                pat = pat.removesuffix("/i").removesuffix("/")

                index = pat.find(".")
                if index == -1:
                    return False

                if index < len(pat) - 1:
                    if pat[index + 1] in ("*", "+", "?", "{"):
                        # like "/VB5!.*/"
                        return False

                if index == 0:
                    # like "/.exe/" which should be "/\.exe/"
                    self.recommendation = self.recommendation_template.format(feature.value)
                    return True

                if pat[index - 1] != "\\":
                    # like "/test.exe/" which should be "/test\.exe/"
                    self.recommendation = self.recommendation_template.format(feature.value)
                    return True

                if pat[index - 1] == "\\":
                    for i, char in enumerate(pat[0:index][::-1]):
                        if char == "\\":
                            continue

                        if i % 2 == 0:
                            # like "/\\\\.\\pipe\\VBoxTrayIPC/"
                            self.recommendation = self.recommendation_template.format(feature.value)
                            return True

                        break

        return False


class FeatureNegativeNumber(Lint):
    name = "feature value is negative"
    recommendation = "specify the number's two's complement representation"
    recommendation_template = (
        "capa treats number features as unsigned values; you may specify the number's two's complement "
        + 'representation; will not match on "{:d}"'
    )

    def check_features(self, ctx: Context, features: list[Feature]):
        for feature in features:
            if isinstance(feature, (capa.features.insn.Number,)):
                assert isinstance(feature.value, int)
                if feature.value < 0:
                    self.recommendation = self.recommendation_template.format(feature.value)
                    return True
        return False


class FeatureNtdllNtoskrnlApi(Lint):
    name = "feature api may overlap with ntdll and ntoskrnl"
    level = Lint.WARN
    recommendation_template = (
        "check if {:s} is exported by both ntdll and ntoskrnl; if true, consider removing {:s} "
        + "module requirement to improve detection"
    )

    def check_features(self, ctx: Context, features: list[Feature]):
        for feature in features:
            if isinstance(feature, capa.features.insn.API):
                assert isinstance(feature.value, str)
                modname, _, impname = feature.value.rpartition(".")

                if modname == "ntdll" and impname in (
                    "LdrGetProcedureAddress",
                    "LdrLoadDll",
                    "NtCreateThread",
                    "NtCreatUserProcess",
                    "NtLoadDriver",
                    "NtQueryDirectoryObject",
                    "NtResumeThread",
                    "NtSuspendThread",
                    "NtTerminateProcess",
                    "NtWriteVirtualMemory",
                    "RtlGetNativeSystemInformation",
                    "NtCreateThreadEx",
                    "NtCreateUserProcess",
                    "NtOpenDirectoryObject",
                    "NtQueueApcThread",
                    "ZwResumeThread",
                    "ZwSuspendThread",
                    "ZwWriteVirtualMemory",
                    "NtCreateProcess",
                    "ZwCreateThread",
                    "NtCreateProcessEx",
                    "ZwCreateThreadEx",
                    "ZwCreateProcess",
                    "ZwCreateUserProcess",
                    "RtlCreateUserProcess",
                    "NtProtectVirtualMemory",
                    "NtEnumerateSystemEnvironmentValuesEx",
                    "NtQuerySystemEnvironmentValueEx",
                    "NtQuerySystemEnvironmentValue",
                ):
                    # ntoskrnl.exe does not export these routines
                    continue

                if modname == "ntoskrnl" and impname in (
                    "PsGetVersion",
                    "PsLookupProcessByProcessId",
                    "KeStackAttachProcess",
                    "ObfDereferenceObject",
                    "KeUnstackDetachProcess",
                    "ExGetFirmwareEnvironmentVariable",
                ):
                    # ntdll.dll does not export these routines
                    continue

                if modname in ("ntdll", "ntoskrnl"):
                    self.recommendation = self.recommendation_template.format(impname, modname)
                    return True
        return False


class FormatSingleEmptyLineEOF(Lint):
    name = "EOF format"
    recommendation = "end file with a single empty line"

    def check_rule(self, ctx: Context, rule: Rule):
        if rule.definition.endswith("\n") and not rule.definition.endswith("\n\n"):
            return False
        return True


class FormatIncorrect(Lint):
    name = "rule format incorrect"
    recommendation_template = "use scripts/capafmt.py or adjust as follows\n{:s}"

    def check_rule(self, ctx: Context, rule: Rule):
        # EOL depends on Git and our .gitattributes defines text=auto (Git handles files it thinks is best)
        # we prefer LF only, but enforcing across OSs seems tedious and unnecessary
        actual = rule.definition.replace("\r\n", "\n")
        expected = capa.rules.Rule.from_yaml(rule.definition, use_ruamel=True).to_yaml()

        if actual != expected:
            diff = difflib.ndiff(actual.splitlines(1), expected.splitlines(True))
            recommendation_template = self.recommendation_template
            self.recommendation = recommendation_template.format("".join(diff))
            return True

        return False


class FormatStringQuotesIncorrect(Lint):
    name = "rule string quotes incorrect"

    def check_rule(self, ctx: Context, rule: Rule):
        events = capa.rules.Rule._get_ruamel_yaml_parser().parse(rule.definition)
        for key in events:
            if isinstance(key, ruamel.yaml.ScalarEvent) and key.value == "string":
                value = next(events)  # assume value is next event
                if not isinstance(value, ruamel.yaml.ScalarEvent):
                    # ignore non-scalar
                    continue
                if value.value.startswith("/") and value.value.endswith(("/", "/i")):
                    # ignore regex for now
                    continue
                if value.style is None:
                    # no quotes
                    self.recommendation = f'add double quotes to "{value.value}"'
                    return True
                if value.style == "'":
                    # single quote
                    self.recommendation = f'change single quotes to double quotes for "{value.value}"'
                    return True

            elif isinstance(key, ruamel.yaml.ScalarEvent) and key.value == "substring":
                value = next(events)  # assume value is next event
                if not isinstance(value, ruamel.yaml.ScalarEvent):
                    # ignore non-scalar
                    continue
                if value.style is None:
                    # no quotes
                    self.recommendation = f'add double quotes to "{value.value}"'
                    return True
                if value.style == "'":
                    # single quote
                    self.recommendation = f'change single quotes to double quotes for "{value.value}"'
                    return True

            else:
                continue

        return False


def run_lints(lints, ctx: Context, rule: Rule):
    for lint in lints:
        if lint.check_rule(ctx, rule):
            yield lint


def run_feature_lints(lints, ctx: Context, features: list[Feature]):
    for lint in lints:
        if lint.check_features(ctx, features):
            yield lint


NAME_LINTS = (
    NameCasing(),
    FilenameDoesntMatchRuleName(),
)


def lint_name(ctx: Context, rule: Rule):
    return run_lints(NAME_LINTS, ctx, rule)


SCOPES_LINTS = (
    MissingScopes(),
    MissingStaticScope(),
    MissingDynamicScope(),
    InvalidStaticScope(),
    InvalidDynamicScope(),
    InvalidScopes(),
)


def lint_scope(ctx: Context, rule: Rule):
    return run_lints(SCOPES_LINTS, ctx, rule)


META_LINTS = (
    MissingNamespace(),
    NamespaceDoesntMatchRulePath(),
    MissingAuthors(),
    MissingExamples(),
    MissingExampleOffset(),
    ExampleFileDNE(),
    UnusualMetaField(),
    LibRuleNotInLibDirectory(),
    LibRuleHasNamespace(),
    InvalidAttckOrMbcTechnique(),
    IncorrectValueType(),
)


def lint_meta(ctx: Context, rule: Rule):
    return run_lints(META_LINTS, ctx, rule)


FEATURE_LINTS = (
    FeatureStringTooShort(),
    FeatureNegativeNumber(),
    FeatureNtdllNtoskrnlApi(),
    FeatureRegexContainsUnescapedPeriod(),
    FeatureRegexRegistryControlSetMatchIncomplete(),
)


def lint_features(ctx: Context, rule: Rule):
    features = get_features(ctx, rule)
    return run_feature_lints(FEATURE_LINTS, ctx, features)


FORMAT_LINTS = (
    FormatSingleEmptyLineEOF(),
    FormatStringQuotesIncorrect(),
    FormatIncorrect(),
)


def lint_format(ctx: Context, rule: Rule):
    return run_lints(FORMAT_LINTS, ctx, rule)


def get_normpath(path):
    return posixpath.normpath(path).replace(os.sep, "/")


def get_features(ctx: Context, rule: Rule):
    # get features from rule and all dependencies including subscopes and matched rules
    features = []
    namespaces = ctx.rules.rules_by_namespace
    deps = [ctx.rules.rules[dep] for dep in rule.get_dependencies(namespaces)]
    for r in [rule] + deps:
        features.extend(get_rule_features(r))
    return features


def get_rule_features(rule):
    features = []

    def rec(statement):
        if isinstance(statement, capa.engine.Statement):
            for child in statement.get_children():
                rec(child)
        else:
            features.append(statement)

    rec(rule.statement)
    return features


LOGIC_LINTS = (
    DoesntMatchExample(),
    StatementWithSingleChildStatement(),
    OrStatementWithAlwaysTrueChild(),
    NotNotUnderAnd(),
    OptionalNotUnderAnd(),
    DuplicateFeatureUnderStatement(),
    RuleDependencyScopeMismatch(),
)


def lint_logic(ctx: Context, rule: Rule):
    return run_lints(LOGIC_LINTS, ctx, rule)


def is_nursery_rule(rule):
    """
    The nursery is a spot for rules that have not yet been fully polished.
    For example, they may not have references to public example of a technique.
    Yet, we still want to capture and report on their matches.
    """
    return rule.meta.get("capa/nursery")


def lint_rule(ctx: Context, rule: Rule):
    logger.debug(rule.name)

    violations = list(
        itertools.chain(
            lint_name(ctx, rule),
            lint_scope(ctx, rule),
            lint_meta(ctx, rule),
            lint_logic(ctx, rule),
            lint_features(ctx, rule),
            lint_format(ctx, rule),
        )
    )

    if len(violations) > 0:
        # don't show nursery rules with a single violation: needs examples.
        # this is by far the most common reason to be in the nursery,
        # and ends up just producing a lot of noise.
        if not (is_nursery_rule(rule) and len(violations) == 1 and violations[0].name == "missing examples"):
            print("")
            print(f'{"    (nursery) " if is_nursery_rule(rule) else ""} {rule.name}')

            for violation in violations:
                print(
                    f"{'    ' if is_nursery_rule(rule) else ''}  {Lint.WARN if is_nursery_rule(rule) else violation.level}: {violation.name}: {violation.recommendation}"
                )
            print("")

    if is_nursery_rule(rule):
        has_examples = not any(v.level == Lint.FAIL and v.name == "missing examples" for v in violations)
        lints_failed = len(
            tuple(
                filter(
                    lambda v: v.level == Lint.FAIL
                    and not (v.name == "missing examples" or v.name == "referenced example doesn't exist"),
                    violations,
                )
            )
        )
        lints_warned = len(
            tuple(
                filter(
                    lambda v: v.level == Lint.WARN
                    or (v.level == Lint.FAIL and v.name == "referenced example doesn't exist"),
                    violations,
                )
            )
        )

        if (not lints_failed) and (not lints_warned) and has_examples:
            print("")
            print(f'{"    (nursery) " if is_nursery_rule(rule) else ""} {rule.name}')
            print(f"      {Lint.WARN}: '[green]no lint failures[/green]': Graduate the rule")
            print("")
    else:
        lints_failed = len(tuple(filter(lambda v: v.level == Lint.FAIL, violations)))
        lints_warned = len(tuple(filter(lambda v: v.level == Lint.WARN, violations)))

    return (lints_failed, lints_warned)


def width(s, count):
    if len(s) > count:
        return s[: count - 3] + "..."
    else:
        return s.ljust(count)


def lint(ctx: Context):
    """
    Returns: dict[string, tuple(int, int)]
      - # lints failed
      - # lints warned
    """
    ret = {}

    source_rules = [rule for rule in ctx.rules.rules.values() if not rule.is_subscope_rule()]
    n_rules: int = len(source_rules)

    with capa.helpers.CapaProgressBar(transient=True, console=capa.helpers.log_console, disable=True) as pbar:
        task = pbar.add_task(description="linting", total=n_rules, unit="rule")
        for rule in source_rules:
            name = rule.name
            pbar.update(task, description=width(f"linting rule: {name}", 48))
            ret[name] = lint_rule(ctx, rule)
            pbar.advance(task)

    return ret


def collect_samples(samples_path: Path) -> dict[str, Path]:
    """
    recurse through the given path, collecting all file paths, indexed by their content sha256, md5, and filename.
    """
    samples = {}
    for path in samples_path.rglob("*"):
        if path.suffix in [".viv", ".idb", ".i64", ".frz", ".fnames"]:
            continue

        try:
            buf = path.read_bytes()
        except IOError:
            continue

        sha256 = hashlib.sha256()
        sha256.update(buf)

        md5 = hashlib.md5()
        md5.update(buf)

        samples[sha256.hexdigest().lower()] = path
        samples[sha256.hexdigest().upper()] = path
        samples[md5.hexdigest().lower()] = path
        samples[md5.hexdigest().upper()] = path
        samples[path.name] = path

    return samples


def main(argv=None):
    if argv is None:
        argv = sys.argv[1:]

    default_samples_path = str(Path(__file__).resolve().parent.parent / "tests" / "data")

    parser = argparse.ArgumentParser(description="Lint capa rules.")
    capa.main.install_common_args(parser, wanted={"tag"})
    parser.add_argument("rules", type=str, action="append", help="Path to rules")
    parser.add_argument("--samples", type=str, default=default_samples_path, help="Path to samples")
    parser.add_argument(
        "--thorough",
        action="store_true",
        help="Enable thorough linting - takes more time, but does a better job",
    )
    args = parser.parse_args(args=argv)

    try:
        capa.main.handle_common_args(args)
    except capa.main.ShouldExitError as e:
        return e.status_code

    if args.debug:
        logging.getLogger("capa").setLevel(logging.DEBUG)
        logging.getLogger("viv_utils").setLevel(logging.DEBUG)
    else:
        logging.getLogger("capa").setLevel(logging.ERROR)
        logging.getLogger("viv_utils").setLevel(logging.ERROR)

    time0 = time.time()

    try:
        rules = capa.main.get_rules_from_cli(args)
    except capa.main.ShouldExitError as e:
        return e.status_code

    logger.info("collecting potentially referenced samples")
    samples_path = Path(args.samples)
    if not samples_path.exists():
        logger.error("samples path %s does not exist", Path(samples_path))
        return -1

    samples = collect_samples(Path(samples_path))

    ctx = Context(samples=samples, rules=rules, is_thorough=args.thorough)

    results_by_name = lint(ctx)
    failed_rules = []
    warned_rules = []
    for name, (fail_count, warn_count) in results_by_name.items():
        if fail_count > 0:
            failed_rules.append(name)

        if warn_count > 0:
            warned_rules.append(name)

    min, sec = divmod(time.time() - time0, 60)
    logger.debug("lints ran for ~ %02d:%02dm", min, sec)

    if warned_rules:
        print("[yellow]rules with WARN:[/yellow]")
        for warned_rule in sorted(warned_rules):
            print("  - " + warned_rule)
        print()

    if failed_rules:
        print("[red]rules with FAIL:[/red]")
        for failed_rule in sorted(failed_rules):
            print("  - " + failed_rule)
        return 1
    else:
        logger.info("[green]no lints failed, nice![/green]")
        return 0


if __name__ == "__main__":
    sys.exit(main())
