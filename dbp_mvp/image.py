"""The passport app container image: built by ACR Tasks from app/ into the MVP's own registry."""

import re
from datetime import datetime, timezone

from . import MvpError
from .config import ROOT

APP = ROOT / "app"
REPOSITORY = "passport-app"
TAG = re.compile(r"(?::[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}|@sha256:[0-9a-f]{64})")
PATH = re.compile(r"[a-z0-9]+(?:[._-][a-z0-9]+)*(?:/[a-z0-9]+(?:[._-][a-z0-9]+)*)*")


def registry_problem(registry):
    """The expected registry: public (the app environment pulls over the internet), Standard, admin user off."""
    registry = registry or {}
    if (registry.get("access") != "Enabled" or registry.get("sku") != "Standard"
            or registry.get("admin") is not False):
        return (f"container registry must be Standard with public access enabled and the admin user disabled "
                f"(found sku {registry.get('sku')}, public access {registry.get('access')}, "
                f"admin user {registry.get('admin')})")
    return None


def show_registry(az, resource_group, name):
    return az("acr", "show", "--resource-group", resource_group, "--name", name, "--query",
              "{access:publicNetworkAccess, sku:sku.name, admin:adminUserEnabled}")


def check_image(image, login_server):
    """Only images from this MVP's registry; the app's identity can pull nothing else."""
    prefix = f"{login_server.lower()}/"
    if not image.startswith(prefix):
        raise MvpError(f"The image must come from this MVP's registry ({prefix}...), not {image}.")
    rest = image[len(prefix):]
    split = min((index for index in (rest.find(":"), rest.find("@")) if index > 0), default=-1)
    if split < 0 or not PATH.fullmatch(rest[:split]) or not TAG.fullmatch(rest[split:]):
        raise MvpError(f"{image} is not a valid image name with a tag, such as {prefix}{REPOSITORY}:20261112-120000.")
    return image


def build(az, resource_group, registry_name, login_server, now=None):
    """Delete previous app images, build app/ with ACR Tasks and return the full image name."""
    problem = registry_problem(show_registry(az, resource_group, registry_name))
    if problem:
        raise MvpError(f"Image build refused: {problem}.")
    repositories = az("acr", "repository", "list", "--name", registry_name)
    if REPOSITORY in repositories:
        az("acr", "repository", "delete", "--name", registry_name, "--repository", REPOSITORY, "--yes")
    tag = (now or datetime.now(timezone.utc)).strftime("%Y%m%d-%H%M%S")
    az.stream("acr", "build", "--registry", registry_name, "--image", f"{REPOSITORY}:{tag}", str(APP))
    return check_image(f"{login_server.lower()}/{REPOSITORY}:{tag}", login_server)
