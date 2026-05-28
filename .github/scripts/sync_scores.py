"""Sync PR scores from the Slop-o-Meter API to a GitHub Projects v2 board."""

from __future__ import annotations

import json
import logging
import os
import sys
import urllib.error
import urllib.request

logger = logging.getLogger(__name__)

_REQUIRED_KEYS = ("pr_number", "readiness_score", "roi_score", "queue_bucket")


class ProjectSyncError(Exception):
    """Unrecoverable sync error (missing project, missing field)."""


class ProjectSyncRateLimitError(ProjectSyncError):
    """GitHub API rate limit exceeded."""


class SlopApiClient:
    """Fetches PR scores from the Slop-o-Meter REST API."""

    def __init__(self, base_url: str, repo: str) -> None:
        self._url = f"{base_url.rstrip('/')}/api/prs?repo={repo}"

    def fetch_scores(self) -> list[dict]:
        """Call GET /api/prs and return parsed JSON list."""
        req = urllib.request.Request(self._url)
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())


class ProjectSyncer:
    """Manages GitHub Projects v2 GraphQL interactions for syncing PR scores."""

    def __init__(self, owner: str, project_number: int, owner_type: str, token: str) -> None:
        self._owner = owner
        self._project_number = project_number
        self._owner_type = owner_type
        self._token = token
        self._metadata: dict | None = None

    def sync_pr(self, pr_node_id: str, readiness_score: int, roi_score: int, bucket: str) -> None:
        """Add PR to project and update score fields."""
        self._ensure_metadata()
        # Add PR to project (idempotent — returns existing item if already present)
        add_result = self._graphql(
            """mutation($projectId: ID!, $contentId: ID!) {
                addProjectV2ItemById(input: {projectId: $projectId, contentId: $contentId}) {
                    item { id }
                }
            }""",
            {"projectId": self._metadata["project_id"], "contentId": pr_node_id},
        )
        item_id = add_result["data"]["addProjectV2ItemById"]["item"]["id"]

        # Update Readiness Score
        self._update_field(item_id, self._metadata["readiness_field_id"], {"number": readiness_score})
        # Update ROI Score
        self._update_field(item_id, self._metadata["roi_field_id"], {"number": roi_score})
        # Update Bucket (skip if unknown value)
        option_id = self._metadata["bucket_options"].get(bucket)
        if option_id:
            self._update_field(item_id, self._metadata["bucket_field_id"], {"singleSelectOptionId": option_id})
        else:
            logger.warning("Unknown bucket '%s', skipping bucket field", bucket)

    def _update_field(self, item_id: str, field_id: str, value: dict) -> None:
        self._graphql(
            """mutation($projectId: ID!, $itemId: ID!, $fieldId: ID!, $value: ProjectV2FieldValue!) {
                updateProjectV2ItemFieldValue(input: {
                    projectId: $projectId, itemId: $itemId, fieldId: $fieldId, value: $value
                }) { projectV2Item { id } }
            }""",
            {
                "projectId": self._metadata["project_id"],
                "itemId": item_id,
                "fieldId": field_id,
                "value": value,
            },
        )

    def _ensure_metadata(self) -> None:
        """Lazy-load project ID and field IDs on first call."""
        if self._metadata is not None:
            return

        # Build query based on owner type
        owner_field = "user" if self._owner_type == "user" else "organization"
        query = f"""query($login: String!, $number: Int!) {{
            {owner_field}(login: $login) {{
                projectV2(number: $number) {{
                    id
                    fields(first: 50) {{
                        nodes {{
                            ... on ProjectV2Field {{ id name }}
                            ... on ProjectV2SingleSelectField {{ id name options {{ id name }} }}
                        }}
                    }}
                }}
            }}
        }}"""
        result = self._graphql(query, {"login": self._owner, "number": self._project_number})

        project = result["data"][owner_field]["projectV2"]
        if project is None:
            raise ProjectSyncError(f"Project {self._owner}/{self._project_number} not found")

        fields = project["fields"]["nodes"]
        metadata: dict = {"project_id": project["id"]}

        # Map field names to IDs
        field_map = {f["name"]: f for f in fields if f.get("name")}
        for name, key in [
            ("Readiness Score", "readiness_field_id"),
            ("ROI Score", "roi_field_id"),
            ("Bucket", "bucket_field_id"),
        ]:
            if name not in field_map:
                raise ProjectSyncError(f"Required field '{name}' not found in project")
            metadata[key] = field_map[name]["id"]

        # Extract bucket option IDs
        bucket_field = field_map["Bucket"]
        metadata["bucket_options"] = {opt["name"]: opt["id"] for opt in bucket_field.get("options", [])}

        self._metadata = metadata

    def _graphql(self, query: str, variables: dict) -> dict:
        """Execute a GraphQL request against the GitHub API."""
        payload = json.dumps({"query": query, "variables": variables}).encode()
        req = urllib.request.Request(
            "https://api.github.com/graphql",
            data=payload,
            headers={
                "Authorization": f"Bearer {self._token}",
                "Content-Type": "application/json",
            },
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())

        if "errors" in data:
            for error in data["errors"]:
                if error.get("type") == "RATE_LIMITED":
                    raise ProjectSyncRateLimitError("GitHub GraphQL rate limit exceeded")
            raise ProjectSyncError(data["errors"][0].get("message", str(data["errors"])))

        return data


def resolve_pr_node_id(owner: str, repo: str, pr_number: int, token: str, cache: dict) -> str | None:
    """Resolve a PR number to its GitHub node_id via the REST API.

    Returns None if the PR is not found (404). Raises ProjectSyncRateLimitError on 403.
    Results are cached in the provided dict for the duration of the run.
    """
    key = f"{owner}/{repo}#{pr_number}"
    if key in cache:
        return cache[key]

    url = f"https://api.github.com/repos/{owner}/{repo}/pulls/{pr_number}"
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
    })
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
            cache[key] = data["node_id"]
            return cache[key]
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        if e.code == 403:
            raise ProjectSyncRateLimitError("GitHub rate limit exceeded")
        raise


def main() -> None:
    """Entry point: fetch scores, resolve node IDs, sync to project board."""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    # Validate required env vars
    required_vars = ("PROJECT_TOKEN", "PROJECT_OWNER", "PROJECT_NUMBER", "OWNER_TYPE", "SLOP_API_URL", "REPO")
    env: dict[str, str] = {}
    for var in required_vars:
        val = os.environ.get(var)
        if not val:
            logger.error("Missing required environment variable: %s", var)
            sys.exit(1)
        env[var] = val

    # Validate OWNER_TYPE
    if env["OWNER_TYPE"] not in ("user", "organization"):
        logger.error("Invalid OWNER_TYPE: '%s' (must be 'user' or 'organization')", env["OWNER_TYPE"])
        sys.exit(1)

    # Validate REPO format
    parts = env["REPO"].split("/")
    if len(parts) != 2 or not parts[0] or not parts[1]:
        logger.error("Malformed REPO: '%s' (must be 'owner/repo')", env["REPO"])
        sys.exit(1)
    repo_owner, repo_name = parts

    # Fetch scores
    client = SlopApiClient(env["SLOP_API_URL"], env["REPO"])
    logger.info("Fetching scores from %s", client._url)
    try:
        scores = client.fetch_scores()
    except (urllib.error.URLError, urllib.error.HTTPError) as e:
        logger.error("Failed to fetch scores: %s", e)
        sys.exit(1)

    # Filter malformed entries
    valid_scores: list[dict] = []
    for entry in scores:
        if all(k in entry for k in _REQUIRED_KEYS):
            valid_scores.append(entry)
        else:
            logger.warning("Skipping malformed entry: %s", entry)

    logger.info("Fetched %d PR scores (%d valid)", len(scores), len(valid_scores))

    # Sync
    syncer = ProjectSyncer(
        owner=env["PROJECT_OWNER"],
        project_number=int(env["PROJECT_NUMBER"]),
        owner_type=env["OWNER_TYPE"],
        token=env["PROJECT_TOKEN"],
    )
    node_id_cache: dict[str, str] = {}
    synced = 0
    skipped = 0
    failed = 0

    for entry in valid_scores:
        pr_number = entry["pr_number"]
        try:
            node_id = resolve_pr_node_id(repo_owner, repo_name, pr_number, env["PROJECT_TOKEN"], node_id_cache)
            if node_id is None:
                logger.warning("PR #%s not found, skipping", pr_number)
                skipped += 1
                continue
            syncer.sync_pr(node_id, int(entry["readiness_score"]), int(entry["roi_score"]), entry["queue_bucket"])
            logger.info(
                "Synced PR #%s: readiness=%s, roi=%s, bucket=%s",
                pr_number, int(entry["readiness_score"]), int(entry["roi_score"]), entry["queue_bucket"],
            )
            synced += 1
        except ProjectSyncRateLimitError:
            logger.error("GitHub rate limit exceeded, aborting")
            sys.exit(1)
        except Exception as e:
            logger.error("Failed to sync PR #%s: %s", pr_number, e)
            failed += 1

    logger.info("Sync complete: %d synced, %d skipped, %d failed", synced, skipped, failed)
    if failed > 0 and synced == 0 and len(valid_scores) > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
