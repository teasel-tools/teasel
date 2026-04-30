from dataclasses import dataclass, field


@dataclass(slots=True)
class RegistryConfig:
    name: str
    url: str


@dataclass(slots=True)
class IndexEntry:
    slug: str
    name: str
    manufacturer: str
    type: str
    interfaces: list[str]
    file: str
    registry: str = ""      # display name
    registry_url: str = ""  # base URL used to fetch this entry


@dataclass(slots=True)
class ConfigParam:
    key: str
    description: str
    required: bool
    type: str
    example: str | None = None
    default: str | None = None


@dataclass(slots=True)
class McpPackage:
    package: str
    distribution: str
    install: str
    command: str
    description: str = ""
    author: str = ""
    language: str = ""
    url: str = ""


@dataclass(slots=True)
class InstrumentDescriptor:
    slug: str
    name: str
    manufacturer: str
    type: str
    interfaces: list[str]
    packages: list[McpPackage]
    params: list[ConfigParam]
    year: int | None = None
    manual: str | None = None
    image: str | None = None
    github: str | None = None
    setup_steps: list[str] = field(default_factory=list)


@dataclass(slots=True)
class ResolvedConfig:
    slug: str
    command: str
    args: list[str]
    env: dict[str, str]
