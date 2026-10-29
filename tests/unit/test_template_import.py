"""Image references reach a shell in the builder VM; only real references pass."""
import pytest

from flint.core._template_build import validate_auth, validate_image


@pytest.mark.parametrize("image", [
    "python:3.12-slim",
    "ghcr.io/acme/app:1.4",
    "registry.example.com:5000/team/app",
    "public.ecr.aws/docker/library/python:3.12-alpine",
    "alpine@sha256:" + "a" * 64,
])
def test_image_references_are_accepted(image):
    assert validate_image(image) == image


@pytest.mark.parametrize("image", [
    "", "python 3", "alpine; rm -rf /", "$(id)", "`id`", "alpine\nRUN x", "-image", "a" * 513,
    "alpine@sha256:short", None, 3,
])
def test_anything_else_is_refused(image):
    with pytest.raises(ValueError):
        validate_image(image)


@pytest.mark.parametrize("auth", [{"username": "u"}, {"username": "u", "password": ""}, "u:p",
                                  {"username": "u", "password": "p", "token": "t"}])
def test_registry_auth_needs_exactly_a_username_and_password(auth):
    with pytest.raises(ValueError):
        validate_auth(auth)
    assert validate_auth({"username": "u", "password": "p"}) == {"username": "u", "password": "p"}
