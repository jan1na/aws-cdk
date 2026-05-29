"""Sync PR scores from the Slop-o-Meter API to a GitHub Projects v2 board.

Only updates scores for PRs already present in the project — never adds or removes rows.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
import urllib.error
import urllib.request

logger = logging.getLogger(__name__)


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

    def get_existing_items(self) -> list[dict]:
        """Query project for all existing items, return list of {item_id, pr_number}."""
        self._ensure_metadata()
        items: list[dict] = []
        cursor = None

        while True:
            after = f', after: "{cursor}"' if cursor else ""
            result = self._graphql(
                f"""query($projectId: ID!) {{
                    node(id: $projectId) {{
                        ... on ProjectV2 {{
                            items(first: 100{after}) {{
                                pageInfo {{ hasNextPage endCursor }}
                                nodes {{
                                    id
                                    content {{
                                        ... on PullRequest {{ number }}
                                    }}
                                }}
                            }}
                        }}
                    }}
                }}""",
                {"projectId": self._metadata["project_id"]},
            )
            page = result["data"]["node"]["items"]
            for node in page["nodes"]:
                content = node.get("content")
                if content and "number" in content:
                    items.append({"item_id": node["id"], "pr_number": content["number"]})
            if not page["pageInfo"]["hasNextPage"]:
                break
            cursor = page["pageInfo"]["endCursor"]

        return items

    def update_scores(self, item_id: str, readiness_score: int, roi_score: int, bucket: str) -> None:
        """Update score fields on an existing project item."""
        self._ensure_metadata()
        self._update_field(item_id, self._metadata["readiness_field_id"], {"number": readiness_score})
        self._update_field(item_id, self._metadata["roi_field_id"], {"number": roi_score})
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

        owner_field = "user" if self._owner_type == "user" else "organization"
        query = f"""query($login: String!, $number: Int!) {{
            {owner_field}(login: $login) {{
                projectV2(number: $number) {{
                    id
                    fields(first: 20) {{
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

        field_map = {f["name"]: f for f in fields if f.get("name")}
        for name, key in [
            ("Readiness Score", "readiness_field_id"),
            ("ROI Score", "roi_field_id"),
            ("Bucket", "bucket_field_id"),
        ]:
            if name not in field_map:
                raise ProjectSyncError(f"Required field '{name}' not found in project")
            metadata[key] = field_map[name]["id"]

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


def main() -> None:
    """Entry point: query existing project items, fetch scores, update matches."""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    required_vars = ("PROJECT_TOKEN", "PROJECT_OWNER", "PROJECT_NUMBER", "OWNER_TYPE", "SLOP_API_URL", "REPO")
    env: dict[str, str] = {}
    for var in required_vars:
        val = os.environ.get(var)
        if not val:
            logger.error("Missing required environment variable: %s", var)
            sys.exit(1)
        env[var] = val

    if env["OWNER_TYPE"] not in ("user", "organization"):
        logger.error("Invalid OWNER_TYPE: '%s' (must be 'user' or 'organization')", env["OWNER_TYPE"])
        sys.exit(1)

    parts = env["REPO"].split("/")
    if len(parts) != 2 or not parts[0] or not parts[1]:
        logger.error("Malformed REPO: '%s' (must be 'owner/repo')", env["REPO"])
        sys.exit(1)

    # Fetch scores from API
    client = SlopApiClient(env["SLOP_API_URL"], env["REPO"])
    logger.info("Fetching scores from %s", client._url)
    try:
        scores = client.fetch_scores()
    except (urllib.error.URLError, urllib.error.HTTPError) as e:
        logger.error("Failed to fetch scores: %s", e)
        sys.exit(1)

    # Build lookup: pr_number -> score entry
    _REQUIRED_KEYS = ("pr_number", "readiness_score", "roi_score", "queue_bucket")
    score_map: dict[int, dict] = {}
    for entry in scores:
        if all(k in entry for k in _REQUIRED_KEYS):
            score_map[int(entry["pr_number"])] = entry
        else:
            logger.warning("Skipping malformed entry: %s", entry)

    logger.info("Fetched %d PR scores (%d valid)", len(scores), len(score_map))

    # Query existing project items
    syncer = ProjectSyncer(
        owner=env["PROJECT_OWNER"],
        project_number=int(env["PROJECT_NUMBER"]),
        owner_type=env["OWNER_TYPE"],
        token=env["PROJECT_TOKEN"],
    )

    try:
        existing_items = syncer.get_existing_items()
    except ProjectSyncRateLimitError:
        logger.error("GitHub rate limit exceeded while querying project items")
        sys.exit(1)

    logger.info("Found %d items in project", len(existing_items))

    # Only update scores for PRs already in the project
    synced = 0
    skipped = 0
    failed = 0

    for item in existing_items:
        pr_number = item["pr_number"]
        if pr_number not in score_map:
            logger.info("PR #%s in project but no score available, skipping", pr_number)
            skipped += 1
            continue
        entry = score_map[pr_number]
        try:
            syncer.update_scores(
                item["item_id"],
                int(entry["readiness_score"]),
                int(entry["roi_score"]),
                entry["queue_bucket"],
            )
            logger.info(
                "Updated PR #%s: readiness=%s, roi=%s, bucket=%s",
                pr_number, int(entry["readiness_score"]), int(entry["roi_score"]), entry["queue_bucket"],
            )
            synced += 1
        except ProjectSyncRateLimitError:
            logger.error("GitHub rate limit exceeded, aborting")
            sys.exit(1)
        except Exception as e:
            logger.error("Failed to update PR #%s: %s", pr_number, e)
            failed += 1

    logger.info("Sync complete: %d updated, %d skipped (no score), %d failed", synced, skipped, failed)
    if failed > 0 and synced == 0 and len(existing_items) > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
