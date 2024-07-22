# This file is Copyright 2019 Volatility Foundation and licensed under the Volatility Software License 1.0
# which is available at https://www.volatilityfoundation.org/license/vsl-v1.0
#

import logging
from typing import Any, Dict, Iterable, List, Tuple

from volatility3.framework import interfaces, renderers
from volatility3.framework.configuration import requirements
from volatility3.framework.interfaces import plugins
from volatility3.framework.layers import resources
from volatility3.framework.renderers import format_hints

vollog = logging.getLogger(__name__)

USE_YARA_X = False

try:
    import yara_x

    USE_YARA_X = True

except ImportError:
    try:
        import yara

        if tuple(int(x) for x in yara.__version__.split(".")) < (3, 8):
            raise ImportError

        vollog.info("Using yara-python module")

    except ImportError:
        vollog.info(
            "Python Yara (>3.8.0) module not found, plugin (and dependent plugins) not available"
        )
        raise


class YaraPythonScanner(interfaces.layers.ScannerInterface):
    _version = (2, 0, 0)

    # yara.Rules isn't exposed, so we can't type this properly
    def __init__(self, rules) -> None:
        super().__init__()
        if rules is None:
            raise ValueError("No rules provided to YaraScanner")
        self._rules = rules
        self.st_object = not tuple(int(x) for x in yara.__version__.split(".")) < (4, 3)

    def __call__(
        self, data: bytes, data_offset: int
    ) -> Iterable[Tuple[int, str, str, bytes]]:
        for match in self._rules.match(data=data):
            if self.st_object:
                for match_string in match.strings:
                    for instance in match_string.instances:
                        yield (
                            instance.offset + data_offset,
                            match.rule,
                            match_string.identifier,
                            instance.matched_data,
                        )
            else:
                for offset, name, value in match.strings:
                    yield (offset + data_offset, match.rule, name, value)

    @staticmethod
    def get_rule(rule):
        return yara.compile(
            sources={"n": f"rule r1 {{strings: $a = {rule} condition: $a}}"}
        )

    @staticmethod
    def from_compiled_file(filepath):
        return yara.load(file=resources.ResourceAccessor().open(filepath, "rb"))

    @staticmethod
    def from_file(filepath):
        return yara.compile(file=resources.ResourceAccessor().open(filepath, "rb"))


class YaraXScanner(interfaces.layers.ScannerInterface):
    _version = (2, 0, 0)

    # yara.Rules isn't exposed, so we can't type this properly
    def __init__(self, rules) -> None:
        super().__init__()
        if rules is None:
            raise ValueError("No rules provided to YaraScanner")
        self._rules = rules

    def __call__(
        self, data: bytes, data_offset: int
    ) -> Iterable[Tuple[int, str, str, bytes]]:
        results = self._rules.scan(data)
        for match in results.matching_rules:
            for match_string in match.patterns:
                for instance in match_string.matches:
                    yield (
                        instance.offset + data_offset,
                        f"{match.namespace}.{match.identifier}",
                        match_string.identifier,
                        data[instance.offset : instance.offset + instance.length],
                    )

    @staticmethod
    def get_rule(rule):
        return yara_x.compile(f"rule r1 {{strings: $a = {rule} condition: $a}}")

    @staticmethod
    def from_compiled_file(filepath):
        return yara_x.Rules.deserialize_from(
            file=resources.ResourceAccessor().open(filepath, "rb")
        )

    @staticmethod
    def from_file(filepath):
        return yara_x.compile(
            resources.ResourceAccessor().open(filepath, "rb").read().decode()
        )


YaraScanner = YaraXScanner if USE_YARA_X else YaraPythonScanner


class YaraScan(plugins.PluginInterface):
    """Scans kernel memory using yara rules (string or file)."""

    _required_framework_version = (2, 0, 0)
    _version = (1, 3, 0)

    @classmethod
    def get_requirements(cls) -> List[interfaces.configuration.RequirementInterface]:
        """Returns the requirements needed to run yarascan directly, combining the TranslationLayerRequirement
        and the requirements from get_yarascan_option_requirements."""
        return cls.get_yarascan_option_requirements() + [
            requirements.TranslationLayerRequirement(
                name="primary",
                description="Memory layer for the kernel",
                architectures=["Intel32", "Intel64"],
            )
        ]

    @classmethod
    def get_yarascan_option_requirements(
        cls,
    ) -> List[interfaces.configuration.RequirementInterface]:
        """Returns the requirements needed for the command lines options used by yarascan. This can
        then also be used by other plugins that are using yarascan. This does not include a
        TranslationLayerRequirement or a ModuleRequirement."""
        return [
            requirements.BooleanRequirement(
                name="insensitive",
                description="Makes the search case insensitive",
                default=False,
                optional=True,
            ),
            requirements.BooleanRequirement(
                name="wide",
                description="Match wide (unicode) strings",
                default=False,
                optional=True,
            ),
            requirements.StringRequirement(
                name="yara_string",
                description="Yara rules (as a string)",
                optional=True,
            ),
            requirements.URIRequirement(
                name="yara_file",
                description="Yara rules (as a file)",
                optional=True,
            ),
            requirements.URIRequirement(
                name="yara_compiled_file",
                description="Yara compiled rules (as a file)",
                optional=True,
            ),
            requirements.IntRequirement(
                name="max_size",
                default=0x40000000,
                description="Set the maximum size (default is 1GB)",
                optional=True,
            ),
        ]

    @classmethod
    def process_yara_options(cls, config: Dict[str, Any]):
        rules = None
        if config.get("yara_string") is not None:
            rule = config["yara_string"]
            if rule[0] not in ["{", "/"]:
                rule = f'"{rule}"'
            if config.get("case", False):
                rule += " nocase"
            if config.get("wide", False):
                rule += " wide ascii"
            rules = YaraScanner.get_rule(rule)
        elif config.get("yara_file") is not None:
            vollog.debug(f"Plain file: {config["yara_file"]} - yara-x: {USE_YARA_X}")
            rules = YaraScanner.from_file(config["yara_file"])
        elif config.get("yara_compiled_file") is not None:
            vollog.debug(
                f"Compiled file: {config["yara_compiled_file"]} - yara-x: {USE_YARA_X}"
            )
            rules = YaraScanner.from_compiled_file(config["yara_compiled_file"])
        else:
            vollog.error("No yara rules, nor yara rules file were specified")
        return rules

    def _generator(self):
        rules = self.process_yara_options(dict(self.config))

        layer = self.context.layers[self.config["primary"]]
        for offset, rule_name, name, value in layer.scan(
            context=self.context, scanner=YaraScanner(rules=rules)
        ):
            yield 0, (format_hints.Hex(offset), rule_name, name, value)

    def run(self):
        return renderers.TreeGrid(
            [
                ("Offset", format_hints.Hex),
                ("Rule", str),
                ("Component", str),
                ("Value", bytes),
            ],
            self._generator(),
        )
