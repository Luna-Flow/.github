#!/usr/bin/env python3
"""Keep the Luna-Flow organization project in sync with open issues and PRs.

- Adds every open issue and pull request of every non-archived repository.
- Fills Status, Tier and Layer only where they are empty, except that an
  open PR still at the "open issue" status moves to the PR status and closed
  or merged items move to the closed status. Manual values are kept.
- Never removes items.

With "public_only": true in the config, private repositories are skipped, so
a public project never receives items from them.

Needs `gh` authenticated with a token that can read every repository and
write the organization project (GH_TOKEN in CI).
"""
import argparse
import json
import os
import subprocess
import sys
from pathlib import Path


def gql(query, **variables):
    body = json.dumps({"query": query, "variables": variables})
    out = subprocess.run(["gh", "api", "graphql", "--input", "-"],
                         input=body, capture_output=True, text=True)
    if out.returncode != 0:
        raise SystemExit(f"GraphQL request failed:\n{out.stderr}{out.stdout}")
    data = json.loads(out.stdout)
    if data.get("errors"):
        raise SystemExit(f"GraphQL errors: {json.dumps(data['errors'], indent=2)}")
    return data["data"]


PROJECT_Q = """
query($org: String!, $number: Int!) {
  organization(login: $org) {
    projectV2(number: $number) {
      id
      fields(first: 50) {
        nodes { ... on ProjectV2SingleSelectField { id name options { id name } } }
      }
    }
  }
}"""

ITEMS_Q = """
query($id: ID!, $after: String) {
  node(id: $id) {
    ... on ProjectV2 {
      items(first: 100, after: $after) {
        pageInfo { hasNextPage endCursor }
        nodes {
          id
          content {
            __typename
            ... on Issue { id state repository { name } }
            ... on PullRequest { id state repository { name } }
          }
          fieldValues(first: 30) {
            nodes {
              ... on ProjectV2ItemFieldSingleSelectValue {
                name
                field { ... on ProjectV2SingleSelectField { name } }
              }
            }
          }
        }
      }
    }
  }
}"""

REPOS_Q = """
query($org: String!, $after: String) {
  organization(login: $org) {
    repositories(first: 100, after: $after) {
      pageInfo { hasNextPage endCursor }
      nodes { name isArchived isPrivate }
    }
  }
}"""

OPEN_Q = """
query($org: String!, $repo: String!, $after: String) {
  repository(owner: $org, name: $repo) {
    %s(states: OPEN, first: 100, after: $after) {
      pageInfo { hasNextPage endCursor }
      nodes { id }
    }
  }
}"""

ADD_M = """
mutation($project: ID!, $content: ID!) {
  addProjectV2ItemById(input: {projectId: $project, contentId: $content}) { item { id } }
}"""

SET_M = """
mutation($project: ID!, $item: ID!, $field: ID!, $option: String!) {
  updateProjectV2ItemFieldValue(input: {
    projectId: $project, itemId: $item, fieldId: $field,
    value: {singleSelectOptionId: $option}
  }) { projectV2Item { id } }
}"""


def paginate(query, path, **variables):
    after = None
    while True:
        data = gql(query, after=after, **variables)
        conn = data
        for key in path:
            conn = conn[key]
        yield from conn["nodes"]
        if not conn["pageInfo"]["hasNextPage"]:
            return
        after = conn["pageInfo"]["endCursor"]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default=Path(__file__).resolve().parent.parent / "project-sync.json")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    config = json.loads(Path(args.config).read_text())
    org = config["org"]

    project = gql(PROJECT_Q, org=org, number=config["project"])["organization"]["projectV2"]
    fields = {f["name"]: f for f in project["fields"]["nodes"] if f}

    def option(field_name, option_name):
        field = fields.get(field_name)
        if field is None:
            raise SystemExit(f"Project has no single-select field {field_name!r}")
        for opt in field["options"]:
            if opt["name"] == option_name:
                return field["id"], opt["id"]
        raise SystemExit(f"Field {field_name!r} has no option {option_name!r}")

    status = config["status"]
    for key in ("open_issue", "open_pull_request", "closed"):
        option(status["field"], status[key])
    for section in ("tier", "layer"):
        for value in set(config[section]["repos"].values()):
            option(config[section]["field"], value)

    items = {}
    for node in paginate(ITEMS_Q, ["node", "items"], id=project["id"]):
        content = node["content"] or {}
        if content.get("__typename") not in ("Issue", "PullRequest"):
            continue
        values = {}
        for v in node["fieldValues"]["nodes"]:
            if v and v.get("field"):
                values[v["field"]["name"]] = v["name"]
        items[content["id"]] = {"item": node["id"], "kind": content["__typename"],
                                "state": content["state"],
                                "repo": content["repository"]["name"], "values": values}

    repos = [r["name"] for r in paginate(REPOS_Q, ["organization", "repositories"], org=org)
             if not r["isArchived"] and not (config.get("public_only") and r["isPrivate"])]

    stats = {"added": 0, "updated": 0, "unmapped": set()}

    def set_value(entry, field_name, option_name, reason):
        field_id, option_id = option(field_name, option_name)
        print(f"  {entry['repo']}: {entry['kind']} {field_name} -> {option_name} ({reason})")
        if not args.dry_run:
            gql(SET_M, project=project["id"], item=entry["item"], field=field_id, option=option_id)
        entry["values"][field_name] = option_name
        stats["updated"] += 1

    for repo in sorted(repos):
        for kind, connection in (("Issue", "issues"), ("PullRequest", "pullRequests")):
            for node in paginate(OPEN_Q % connection, ["repository", connection], org=org, repo=repo):
                if node["id"] in items:
                    continue
                print(f"add {repo} {kind} {node['id']}")
                item_id = "dry-run" if args.dry_run else \
                    gql(ADD_M, project=project["id"], content=node["id"])["addProjectV2ItemById"]["item"]["id"]
                items[node["id"]] = {"item": item_id, "kind": kind, "state": "OPEN",
                                     "repo": repo, "values": {}}
                stats["added"] += 1

    for entry in items.values():
        current = entry["values"].get(status["field"])
        if entry["state"] == "OPEN":
            if entry["kind"] == "Issue" and current is None:
                set_value(entry, status["field"], status["open_issue"], "open issue")
            elif entry["kind"] == "PullRequest" and current in (None, status["open_issue"]):
                set_value(entry, status["field"], status["open_pull_request"], "open PR")
        elif current != status["closed"]:
            set_value(entry, status["field"], status["closed"], entry["state"].lower())
        for section in ("tier", "layer"):
            field_name = config[section]["field"]
            if entry["values"].get(field_name) is not None:
                continue
            value = config[section]["repos"].get(entry["repo"])
            if value is None:
                stats["unmapped"].add(entry["repo"])
                continue
            set_value(entry, field_name, value, "repository default")

    summary = (f"{'Dry run: ' if args.dry_run else ''}{stats['added']} items added, "
               f"{stats['updated']} field values set, {len(items)} issues/PRs tracked.")
    if stats["unmapped"]:
        summary += f" Repositories missing from project-sync.json: {', '.join(sorted(stats['unmapped']))}."
    print(summary)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as f:
            f.write(summary + "\n")


if __name__ == "__main__":
    sys.exit(main())
