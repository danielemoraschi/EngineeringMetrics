"""Helpers to access jira issue fields"""
import datetime

from dateutil import parser
from cachetools import cached
from jira import JIRAError

import ia.common.helpers as h
from ia.common.jira.links import get_external_dependencies, is_internal
from ia.common.jira.sprint import Sprint

issues_cache = {}

STATUS_FIELD = "status"
STATE_FIELD = "state"


@cached(cache=issues_cache)
def get_issue_by_key(jira_obj, issue_key, fields=None, expand=None):
    issue_details = []
    jql = f"'key'='{issue_key}'"
    try:
        issue_details = jira_obj.search_issues(jql, maxResults=1, fields=fields, expand=expand)
    except JIRAError as error:
        # print(f"error_code:{error.status_code}, error_msg:{error.text}")
        # import traceback, sys
        # traceback.print_stack(file=sys.stdout)
        pass
    issue = issue_details[0] if len(issue_details) else None
    return IssueCache(jira_obj, issue) if issue else None


@cached(cache=issues_cache)
def search_issues(jira_access, jql, fields=None, expand=None):
    found_issues = jira_access.search_issues(jql, maxResults=500, fields=fields, expand=expand)

    issues = []
    for iss in found_issues:
        issue_wrapper = IssueCache(jira_access, iss)
        issues.append(issue_wrapper)
        # add to cache
        issues_cache[(jira_access, issue_wrapper.key, fields, expand)] = issue_wrapper
    return issues


class IssueCache:
    def __init__(self, jira_access, issue):
        self._jira = jira_access
        self._issue = issue
        self._epic_issue = None
        self._epic_name = None
        self._linked_issues = {}
        self._status_log = None

    def get_jira_access(self):
        return self._jira

    def get_issue(self):
        return self._issue

    def get_fields(self):
        return self.issue.fields

    def get_project(self):
        return self.fields.project

    def get_status(self):
        return self.fields.status.name

    def get_summary(self):
        return self.fields.summary

    def get_sprints(self):
        sprints = []
        sprint_raw_list = ""
        try:
            sprint_raw_list = self.issue.fields.customfield_11220
            if isinstance(sprint_raw_list, list):
                for r in sprint_raw_list:
                    if isinstance(r, str):
                        sprint = Sprint(r)
                        sprints.append(sprint)
        except Exception as e:
            print(f"{self.key}: {e}")
            print(f"raw: {sprint_raw_list}")

        return sprints

    def get_epic_name(self):
        if not self._epic_name:
            try:
                epic_key = self.issue.fields.customfield_12120
                if epic_key is not None:
                    self._epic_issue = get_issue_by_key(self._jira, epic_key)
                    self._epic_name = self._epic_issue.fields.customfield_12121
            except Exception:
                # keep defaults
                pass
        return self._epic_name

    def get_linked_issues(self):
        return self._linked_issues

    def load_linked_issues(self, max_level=2, filter_out_status=("Done")):
        load_external_issues(self, max_level, filter_out_status)
        return self._linked_issues

    def get_key(self):
        return self.issue.key

    def get_created_day(self):
        return parser.parse(self.issue.fields.created)

    def get_resolution_day(self):
        if self.issue.fields.resolutiondate:
            return parser.parse(self.issue.fields.resolutiondate)
        return None

    def calc_lead_time(self) -> int:
        """The number of business days an issue took to resolve. 
        Returns:
            Number of days to resolve issue or -1 if issue is not resolved.
        """
        duration = -1
        if self.resolution_date:
            duration = h.business_days(self.created, self.resolution_date).days

        return duration

    def calc_cycle_time(
        self, begin_status: str = "In Progress", resolution_status: str = "Done"
    ) -> int:
        """The number of business days an issue took to resolve.

        Args:
            begin_status: when the work was started on this issue
            resolution_status: used in the case no resolution date is set

        Returns:
            Number of days to resolve issue or -1 if issue is not resolved.
        """

        start_date = None
        for log in self.status_log:
            if log[STATE_FIELD] == begin_status:
                start_date = log["at"]
                break
        if start_date == None:
            start_date = self.created

        resolution_date = None
        for log in self.status_log:
            if log[STATE_FIELD] == resolution_status:
                resolution_date = log["at"]
                break

        if resolution_date == None and self.resolution_date:
            resolution_date = self.resolution_date

        if resolution_date != None:
            return h.business_days(start_date, resolution_date).days

        return -1

    def get_status_log(self) -> list:
        if self._status_log is None:
            self._status_log = []
            self._status_log.append(dict(at=self.created, state="Created"))
            for history in self.issue.changelog.histories:
                try:
                    change_date = parser.parse(history.created)

                    for item in history.items:
                        if item.field.upper() == STATUS_FIELD.upper():
                            self._status_log.append(
                                dict(at=change_date, state=str(item.toString))
                            )
                except AttributeError:
                    pass
        return self._status_log

    jira_access = property(get_jira_access)
    issue = property(get_issue)
    fields = property(get_fields)
    project = property(get_project)
    epic_name = property(get_epic_name)
    key = property(get_key)
    linked_issues = property(get_linked_issues)
    status = property(get_status)
    status_log = property(get_status_log)
    summary = property(get_summary)
    sprints = property(get_sprints)
    created = property(get_created_day)
    resolution_date = property(get_resolution_day)


def load_external_issues(issue_cache, max_level, filter_out_status):
    load_external_issues.depth_level = 0

    def load_links(issue_cache):
        jira_access = issue_cache._jira
        links = []
        linked_issues = {}
        issue = issue_cache.issue
        links = get_external_dependencies(issue)
        links += list(get_indirect_external_dependencies(jira_access, issue, links))

        for l in links:
            l_issue = get_issue_by_key(jira_access, l.key)
            if l_issue.status not in filter_out_status:
                linked_issues[l.key] = l_issue

        if load_external_issues.depth_level < max_level:
            load_external_issues.depth_level += 1
            for l_issue_cache in linked_issues.values():
                l_issue_cache._linked_issues = load_links(l_issue_cache)
        return linked_issues

    issue_cache._linked_issues = load_links(issue_cache)


def get_indirect_external_dependencies(jira_access, issue, links_with_external_deps) -> set:
    seen = set()
    project_name = issue.fields.project.key
    keys_with_external_dep = [l.key for l in links_with_external_deps]

    def walk(jira_access, issue, keys_with_external_dep):
        links = set()
        seen.add(issue.key)
        for link in issue.fields.issuelinks:
            l_type = "inwardIssue" if hasattr(link, "inwardIssue") else "outwardIssue"
            link.key = getattr(link, l_type).key

            if is_internal(link.key, project_name) and (
                link.type.name in ("Dependancy", "Dependency")
                and l_type == "outwardIssue"
                or link.type.name == "Blocks"
                and l_type == "inwardIssue"
            ):
                if link.key in keys_with_external_dep:
                    print(f"Found indirect dependency: {link.key}")
                    links.add(link)
                else:
                    # Recursion to check other internal links which may have dependency on the issue with external dependency
                    # print(link.key)
                    if link.key not in seen:
                        l_issue = get_issue_by_key(jira_access, link.key)

                        links = links.union(walk(jira_access, l_issue, keys_with_external_dep))
        return links

    return walk(jira_access, issue, keys_with_external_dep)
