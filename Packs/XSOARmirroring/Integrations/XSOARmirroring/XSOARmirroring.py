import json
from datetime import timedelta
from typing import Any

import dateparser
import demistomock as demisto
import requests
import urllib3
from CommonServerPython import *

# Disable insecure warnings
urllib3.disable_warnings()

""" CONSTANTS """


MAX_INCIDENTS_TO_FETCH = 100
FIELDS_TO_COPY_FROM_REMOTE_INCIDENT = [
    "name",
    "rawName",
    "severity",
    "occurred",
    "modified",
    "roles",
    "type",
    "rawType",
    "status",
    "reason",
    "created",
    "closed",
    "sla",
    "labels",
    "attachment",
    "details",
    "openDuration",
    "lastOpen",
    "owner",
    "closeReason",
    "rawCloseReason",
    "closeNotes",
    "playbookId",
    "dueDate",
    "reminder",
    "runStatus",
    "notifyTime",
    "phase",
    "rawPhase",
    "CustomFields",
    "category",
    "rawCategory",
]

MIRROR_DIRECTION = {"None": None, "Incoming": "In", "Outgoing": "Out", "Incoming And Outgoing": "Both"}
XSOAR_DATE_FORMAT = "%Y-%m-%dT%H:%M:%S.%fZ"
MIRROR_RESET = "XSOARMirror_mirror_reset"

""" CLIENT CLASS """


class Client(BaseClient):
    def search_incidents(
        self, query: str | None, max_results: int | None, start_time: Union[str, int], page: int = None, field: str = "id"
    ) -> list[dict[str, Any]]:
        data = remove_empty_elements(
            {
                "filter": {
                    "query": query,
                    "size": max_results or 10,
                    "page": page,
                    "fromDate": start_time,
                    "sort": [{"field": field, "asc": True}],
                }
            }
        )
        return self._http_request(method="POST", url_suffix="/incidents/search", json_data=data).get("data")

    def get_incident(self, incident_id: str) -> dict[str, Any]:
        return self._http_request(method="GET", url_suffix=f"/incident/load/{incident_id}")

    def get_file_entry(self, entry_id: str) -> requests.Response:
        return self._http_request(method="GET", url_suffix=f"/entry/download/{entry_id}", resp_type="content")

    def get_incident_entries(
        self,
        incident_id: str,
        from_date: int | None,
        max_results: int | None,
        categories: list[str] | None,
        tags: list[str] | None,
        tags_and_operator: bool,
    ) -> list[dict[str, Any]]:
        data = {
            "pageSize": max_results or 50,
            "fromTime": timestamp_to_datestring(from_date),
            "categories": categories,
            "tags": tags,
            "tagsAndOperator": tags_and_operator,
        }
        inv_with_entries = self._http_request(method="POST", url_suffix=f"/investigation/{incident_id}", json_data=data)
        return inv_with_entries.get("entries", [])

    def get_incident_fields(self) -> list[dict[str, Any]]:
        return self._http_request(method="GET", url_suffix="/incidentfields")

    def get_incident_types(self) -> list[dict[str, Any]]:
        return self._http_request(method="GET", url_suffix="/incidenttype")

    def update_incident(self, incident: dict[str, Any]) -> dict[str, Any]:
        return self._http_request(method="POST", url_suffix="/incident", json_data=incident)

    def close_incident(
        self, incident_id: str, incident_ver: int, close_reason: str | None, close_notes: str | None
    ) -> dict[str, Any]:
        return self._http_request(
            method="POST",
            url_suffix="/incident/close",
            json_data={"id": incident_id, "version": incident_ver, "closeNotes": close_notes, "closeReason": close_reason},
        )

    def add_incident_entry(self, incident_id: str | None, entry: dict[str, Any]):
        if entry.get("type") == 3:
            path_res = demisto.getFilePath(entry.get("id"))
            full_file_name = path_res.get("name")

            with open(path_res.get("path"), "rb") as file_to_send:
                self._http_request(
                    method="POST",
                    url_suffix=f"/entry/upload/{incident_id}",
                    files={"file": (full_file_name, file_to_send, "application/octet-stream")},
                )

        if not entry.get("note", False):
            demisto.debug(f'the entry has inv_id {incident_id}\nformat {entry.get("format")}\n contents {entry.get("contents")}')
            self._http_request(
                method="POST",
                url_suffix="/entry/formatted",
                json_data={"contents": entry.get("contents"), "format": entry.get("format"), "investigationId": incident_id},
            )
        else:
            demisto.debug(f'the entry has inv_id {incident_id}\ndata {entry.get("date")}\nmarkdown {entry.get("markdown")}')
            entry_format = entry.get("format") == "markdown"
            self._http_request(
                method="POST",
                url_suffix="/entry/note",
                json_data={"investigationId": incident_id, "data": entry.get("contents", "False"), "markdown": entry_format},
            )


""" HELPER FUNCTIONS """


def validate_and_prepare_basic_params(params: dict):
    """
    Validated and then prepares the API Key, API ID, and URL.

    Args:
        params (dict): The params dictionary.

    Return:
        str: the API key ID
        str: the API key secret
        str: the URL to the server with `xsoar` suffix if one is needed
    """
    api_key = params.get("credentials_api_key", {}).get("password") or params.get("apikey")
    api_key_id = params.get("credentials_api_key", {}).get("identifier")
    base_url = params.get("url", "")
    if not api_key:
        raise DemistoException("API Key must be provided.")

    # For cloud environments an 'xsoar' suffix must be added
    if api_key_id:
        base_url = base_url if base_url.endswith("/xsoar") else base_url + "/xsoar"

    return api_key_id, api_key, base_url


def arg_to_timestamp(arg: str, arg_name: str, required: bool = False):
    """Converts an XSOAR argument to a timestamp (seconds from epoch)

    This function is used to quickly validate an argument provided to XSOAR
    via ``demisto.args()`` into an ``int`` containing a timestamp (seconds
    since epoch). It will throw a ValueError if the input is invalid.
    If the input is None, it will throw a ValueError if required is ``True``,
    or ``None`` if required is ``False.

    :type arg: ``Any``
    :param arg: argument to convert

    :type arg_name: ``str``
    :param arg_name: argument name

    :type required: ``bool``
    :param required:
        throws exception if ``True`` and argument provided is None

    :return:
        returns an ``int`` containing a timestamp (seconds from epoch) if conversion works
        returns ``-1`` if arg is ``None`` and required is set to ``False``
        otherwise throws an Exception
    :rtype: ``Optional[int]``
    """

    if arg is None:
        if required is True:
            raise ValueError(f'Missing "{arg_name}"')
        return -1

    if isinstance(arg, str) and arg.isdigit():
        # timestamp is a str containing digits - we just convert it to int
        return int(arg)
    if isinstance(arg, str):
        # we use dateparser to handle strings either in ISO8601 format, or
        # relative time stamps.
        # For example: format 2019-10-23T00:00:00 or "3 days", etc
        date = dateparser.parse(arg, settings={"TIMEZONE": "UTC"})
        if date is None:
            # if d is None it means dateparser failed to parse it
            raise ValueError(f"Invalid date: {arg_name}")

        return int(date.timestamp())
    if isinstance(arg, int | float):
        # Convert to int if the input is a float
        return int(arg)
    raise ValueError(f'Invalid date: "{arg_name}"')


def test_module(client: Client, first_fetch_time: str) -> str:
    """Tests API connectivity and authentication

    Returning 'ok' indicates that the integration works like it is supposed to.
    Connection to the service is successful.
    Raises exceptions if something goes wrong.

    :type client: ``Client``
    :param client: XSOAR client to use

    :type first_fetch_time: ``str``
    :param first_fetch_time: First fetch time parameter value.

    :return: 'ok' if test passed, anything else will fail the test.
    :rtype: ``str``
    """

    try:
        client.search_incidents(max_results=1, start_time=first_fetch_time, query=None)
        return "ok"
    except DemistoException as e:
        if "Forbidden" in str(e):
            return "Authorization Error: make sure API Key is correctly set"
        else:
            raise e


def fetch_incidents(
    client: Client,
    max_results: int,
    last_run: dict[str, Union[str, int, list[str]]],
    last_fetch: Union[str, int],
    first_fetch_time: Union[int, str],
    query: str | None,
    mirror_direction: str,
    mirror_tag: list[str],
    mirror_playbook_id: bool = False,
    fetch_incident_history: bool = False,
) -> tuple[dict[str, Union[list[dict[Any, Any]], str, Any]], list[dict[str, Any]]]:
    """This function retrieves new incidents every interval (default is 1 minute).

    :type client: ``Client``
    :param client: XSOAR client to use

    :type max_results: ``int``
    :param max_results: Maximum numbers of incidents per fetch

    :type last_run: ``Dict[str, Union[str, int, List[str]]],``
    :param last_run:
        A dict with a key containing the latest incident created time we got
        from last fetch

    :type first_fetch_time: ``Optional[int, str]``
    :param first_fetch_time:
        If last_run is None (first time we are fetching), it contains
        the timestamp in milliseconds on when to start fetching incidents

    :type query: ``Optional[str]``
    :param query:
        query to fetch the relevant incidents

    :type mirror_direction: ``str``
    :param mirror_direction:
        Mirror direction for the fetched incidents

    :type mirror_playbook_id: `bool`
    :param mirror_playbook_id:
        When set to false, mirrored incidents will have a blank playbookId value,
         causing the receiving machine to run the default playbook of the incident type.

    :type fetch_incident_history: `bool`
    :param fetch_incident_history:
        When set to True, all incidents fetched will be added to a context data list
        which will be used in  get_remote_data_command

    :type mirror_tag: ``List[str]``
    :param mirror_tag:
        The tags that you will mirror out of the incident.

    :return:
        A tuple containing two elements:
            next_run (``Dict[str, str]``): Contains the timestamp that will be
                    used in ``last_run`` on the next fetch.
            incidents (``List[dict]``): List of incidents that will be created in XSOAR

    :rtype: ``Tuple[Dict[str, str], List[dict]]``
    """
    demisto.debug(f"last run is: {last_run}")
    last_fetched_incidents: list = last_run.get("last_fetched_incidents", [])  # type: ignore
    if not last_fetch:
        last_fetch = first_fetch_time  # type: ignore
    else:
        demisto.debug(
            "Trying to convert the last_fetch to int, and convert it to date string if succeed. "
            "This is for preventing backward compatibility breakage."
        )
        try:
            last_fetch = int(last_fetch)
            last_fetch = datetime.fromtimestamp(last_fetch).strftime(XSOAR_DATE_FORMAT)
        except Exception:
            pass

    latest_created_time = dateparser.parse(last_fetch)  # type: ignore[arg-type]
    incidents_result: list[dict[str, Any]] = []
    if query:
        query += f' and created:>="{last_fetch}"'
    else:
        query = f'created:>="{last_fetch}"'

    demisto.debug(f"XSOAR Mirroring: Fetching incidents since last fetch: {last_fetch}")

    incidents, last_fetched_incidents, last_fetched_incident_time = get_and_dedup_incidents(
        client, last_fetched_incidents, query, max_results, last_fetch
    )
    demisto.debug(f"last_fetched_incident_time: {last_fetched_incident_time}")
    if fetch_incident_history:
        integration_context = get_integration_context()
        incident_mirror_reset: dict = {incident["id"]: True for incident in incidents}
        if incident_mirror_reset:
            if isinstance(integration_context.get(MIRROR_RESET), dict):
                demisto.debug(
                    f"Adding incidents id: {incident_mirror_reset} "
                    f"to integration context: {integration_context.get(MIRROR_RESET)}"
                )
                integration_context[MIRROR_RESET].update(incident_mirror_reset)
            else:
                demisto.debug(f"Set integration context to: {incident_mirror_reset}")
                integration_context[MIRROR_RESET] = incident_mirror_reset
            set_to_integration_context_with_retries(context=integration_context)

    for incident in incidents:
        incident_result: dict[str, Any] = {}
        incident_result["dbotMirrorDirection"] = MIRROR_DIRECTION.get(mirror_direction)  # type: ignore
        incident["dbotMirrorInstance"] = demisto.integrationInstance()
        incident_result["dbotMirrorTags"] = mirror_tag if mirror_tag else None  # type: ignore
        incident_result["dbotMirrorId"] = incident["id"]

        if mirror_playbook_id:
            fields = FIELDS_TO_COPY_FROM_REMOTE_INCIDENT
        else:
            fields = [field for field in FIELDS_TO_COPY_FROM_REMOTE_INCIDENT if field != "playbookId"]

        for key, value in incident.items():
            if key in fields:
                incident_result[key] = value

        incident_result["rawJSON"] = json.dumps(incident)

        file_attachments = []
        # When demisto.command() == 'test-module' we can't write files since we are not running in a playground.
        if (
            demisto.command() != "test-module"
            and incident.get("attachment")
            and len(incident.get("attachment", [])) > 0
            and incident.get("investigationId")
        ):
            entries = client.get_incident_entries(
                incident_id=incident["investigationId"],  # type: ignore
                from_date=0,
                max_results=10,
                categories=["attachments"],
                tags=None,
                tags_and_operator=False,
            )

            for entry in entries:
                if "file" in entry and entry.get("file"):
                    file_entry_content = client.get_file_entry(entry.get("id"))  # type: ignore
                    file_result = fileResult(entry["file"], file_entry_content)
                    if any(attachment.get("name") == entry["file"] for attachment in incident.get("attachment", [])):
                        if file_result["Type"] == EntryType.ERROR:
                            raise Exception(f"Error getting attachment: {file_result.get('Contents', '')!s}")

                        file_attachments.append({"path": file_result.get("FileID", ""), "name": file_result.get("File", "")})

        incident_result["attachment"] = file_attachments
        incidents_result.append(incident_result)
        incident_created_time = dateparser.parse(incident.get("created"), settings={"TIMEZONE": "Z"})  # type: ignore[arg-type]
        # Update last run and add incident if the incident is newer than last fetch
        if incident_created_time > latest_created_time:  # type: ignore[operator]
            latest_created_time = incident_created_time

    # Save the next_run as a dict with the last_fetch key to be stored
    next_run = {
        "last_fetch": (latest_created_time)  # type: ignore[operator]
        .strftime(XSOAR_DATE_FORMAT),  # type: ignore[union-attr,operator]
        "last_fetched_incidents": last_fetched_incidents,
    }
    demisto.debug(f"XSOAR Mirroring: Setting next run to: {next_run}")
    return next_run, incidents_result


def search_incidents_command(client: Client, args: dict[str, Any]) -> CommandResults:
    """xsoar-search-incidents command: Search XSOAR incidents

    :type client: ``Client``
    :param Client: XSOAR client to use

    :type args: ``Dict[str, Any]``
    :param args:
        all command arguments, usually passed from ``demisto.args()``.
        ``args['query']`` query to search incidents
        ``args['start_time']``  start time as ISO8601 date or seconds since epoch
        ``args['max_results']`` maximum number of results to return
        ``args['columns']`` which columns to display in the table

    :return:
        A ``CommandResults`` object that is then passed to ``return_results``,
        that contains incidents

    :rtype: ``CommandResults``
    """

    query = args.get("query")
    start_date = dateparser.parse(args.get("start_time", "3 days")).strftime(XSOAR_DATE_FORMAT)  # type: ignore[union-attr]
    max_results = arg_to_number(arg=args.get("max_results"), arg_name="max_results", required=False)
    incidents = client.search_incidents(query=query, start_time=start_date, max_results=max_results)
    if not incidents:
        incidents = []

    columns = args.get("columns")
    if columns != "all":
        columns = argToList(columns)
        human_readable = tableToMarkdown("Search Results:", incidents, columns)
    else:
        human_readable = tableToMarkdown("Search Results:", incidents)

    return CommandResults(
        readable_output=human_readable, outputs_prefix="XSOAR.Incident", outputs_key_field="id", outputs=incidents
    )


def get_incident_command(client: Client, args: dict[str, Any]) -> CommandResults:
    """xsoar-get-incident command: Returns an incident and all entries with given categories and tags

    :type client: ``Client``
    :param Client: XSOAR client to use

    :type args: ``Dict[str, Any]``
    :param args:
        all command arguments, usually passed from ``demisto.args()``.
        ``args['id']`` incident ID to return
        ``args['from_date']`` only return entries after last update
        ``args['categories']`` only return entries with given categories
        ``args['tags']`` only return entries with given tags

    :return:
        A ``CommandResults`` object that is then passed to ``return_results``,
        that contains an alert

    :rtype: ``CommandResults``
    """

    incident_id = args.get("id", None)
    if not incident_id:
        raise ValueError("id not specified")

    from_date_arg = args.get("from_date", "3 days")
    from_date = arg_to_timestamp(arg=from_date_arg, arg_name="from_date", required=False)
    max_results = arg_to_number(arg=args.get("max_results"), arg_name="max_results", required=False)
    categories = args.get("categories", None)
    if categories:
        categories = categories.split(",")

    tags = args.get("tags", None)
    if tags:
        tags = tags.split(",")

    incident = client.get_incident(incident_id=incident_id)
    incident_title = incident.get("name", incident_id)

    readable_output = tableToMarkdown(f"Incident {incident_title}", incident)

    entries = client.get_incident_entries(
        incident_id=incident_id,
        from_date=from_date * 1000,
        max_results=max_results,
        categories=categories,
        tags=tags,
        tags_and_operator=True,
    )

    readable_output += "\n\n" + tableToMarkdown(
        f"Last entries since {timestamp_to_datestring(from_date * 1000)}", entries, removeNull=True
    )

    return CommandResults(
        readable_output=readable_output, outputs_prefix="XSOAR.Incident", outputs_key_field="incident_id", outputs=incident
    )


def get_mapping_fields_command(client: Client) -> GetMappingFieldsResponse:
    """get-mapping-fields command: Returns the list of fields for an incident type

    :type client: ``Client``
    :param Client: XSOAR client to use

    :type args: ``Dict[str, Any]``
    :param args:
        all command arguments, usually passed from ``demisto.args()``.
        ``args['type']`` incident type to retrieve fields for

    :return:
        A ``Dict[str, Any]`` object with keys as field names and description as values

    :rtype: ``Dict[str, Any]``
    """
    all_mappings = GetMappingFieldsResponse()
    incident_fields: list[dict] = client.get_incident_fields()
    types = client.get_incident_types()
    for incident_type_obj in types:
        custom_fields = {}
        incident_type_name: str = incident_type_obj.get("name")  # type: ignore
        incident_type_scheme = SchemeTypeMapping(type_name=incident_type_name)
        demisto.debug(f'Collecting incident mapping for incident type - "{incident_type_name}"')

        for field in incident_fields:
            if (
                field.get("group") == 0
                and field.get("cliName") is not None
                and (
                    field.get("associatedToAll")
                    or incident_type_name  # type: ignore
                    in field.get("associatedTypes")
                )
            ):  # type: ignore
                if field.get("content") or not field.get("system"):
                    custom_fields[field.get("cliName")] = f"{field.get('name')} - {field.get('type')}"
                    continue
                incident_type_scheme.add_field(
                    name=field.get("cliName"), description=f"{field.get('name')} - {field.get('type')}"
                )
        if custom_fields:
            incident_type_scheme.add_field(name="CustomFields", description=custom_fields)
        all_mappings.add_scheme_type(incident_type_scheme)

    default_scheme = SchemeTypeMapping(type_name="Default Mapping")
    demisto.debug("Collecting the default incident scheme")
    custom_fields = {}
    for field in incident_fields:
        if field.get("group") == 0 and field.get("associatedToAll"):
            if field.get("content") or not field.get("system"):
                custom_fields[field.get("cliName")] = f"{field.get('name')} - {field.get('type')}"
                continue
            default_scheme.add_field(name=field.get("cliName"), description=f"{field.get('name')} - {field.get('type')}")

    if custom_fields:
        default_scheme.add_field(name="CustomFields", description=custom_fields)
    all_mappings.add_scheme_type(default_scheme)
    return all_mappings


def demisto_debug(msg):
    if demisto.params().get("debug_mode"):
        demisto.info(msg)
    else:
        demisto.debug(msg)


def get_remote_data_command(client: Client, args: dict[str, Any], params: dict[str, Any]) -> GetRemoteDataResponse:
    """get-remote-data command: Returns an updated incident and entries

    :type client: ``Client``
    :param Client: XSOAR client to use

    :type args: ``Dict[str, Any]``
    :param args:
        all command arguments, usually passed from ``demisto.args()``.
        ``args['id']`` incident id to retrieve
        ``args['lastUpdate']`` when was the last time we retrieved data

    :return:
        A ``List[Dict[str, Any]]`` first entry is the incident (which can be completely empty) and others are the new entries

    :rtype: ``List[Dict[str, Any]]``
    """
    demisto_debug(f"##### get-remote-data args: {json.dumps(args, indent=4)}")
    incident = None
    try:
        args["lastUpdate"] = arg_to_timestamp(
            arg=args.get("lastUpdate"),  # type: ignore
            arg_name="lastUpdate",
            required=True,
        )
        remote_args = GetRemoteDataArgs(args)
        demisto_debug(f"Getting update for remote [{remote_args.remote_incident_id}]")

        categories = argToList(params.get("categories", None))
        tags = argToList(params.get("tags", None))

        incident = client.get_incident(incident_id=remote_args.remote_incident_id)  # type: ignore
        # If incident was modified before we last updated, no need to return it
        modified = arg_to_timestamp(
            arg=incident.get("modified"),  # type: ignore
            arg_name="modified",
            required=False,
        )
        occurred = arg_to_timestamp(
            arg=incident.get("occurred"),  # type: ignore
            arg_name="occurred",
            required=False,
        )

        if datetime.fromtimestamp(modified) - datetime.fromtimestamp(occurred) < timedelta(minutes=1) and datetime.fromtimestamp(
            remote_args.last_update
        ) - datetime.fromtimestamp(modified) < timedelta(minutes=1):
            remote_args.last_update = occurred + 1
            # in case new entries created less than a minute after incident creation

        demisto_debug(f"tags: {tags}")
        demisto_debug(f"categories: {categories}")
        integration_context = get_integration_context()
        XSOARMirror_mirror_reset: dict = json.loads(integration_context.get(MIRROR_RESET, "{}"))
        is_incident_update_after_reset = False
        if XSOARMirror_mirror_reset:
            is_incident_update_after_reset = XSOARMirror_mirror_reset.get(remote_args.remote_incident_id, None)
        from_date = 0 if is_incident_update_after_reset else remote_args.last_update * 1000
        demisto.debug(f"Requesting update for incident id {remote_args.remote_incident_id} from date: {from_date}")
        entries = client.get_incident_entries(
            incident_id=remote_args.remote_incident_id,  # type: ignore
            from_date=from_date,
            max_results=100,
            categories=categories,
            tags=tags,
            tags_and_operator=True,
        )
        if is_incident_update_after_reset:
            del XSOARMirror_mirror_reset[remote_args.remote_incident_id]
            integration_context[MIRROR_RESET] = XSOARMirror_mirror_reset
            set_to_integration_context_with_retries(context=integration_context)
            demisto.debug(f"Removed incident id: {remote_args.remote_incident_id} from XSOARMirror_mirror_reset\
                context data list.")

        formatted_entries = []
        # file_attachments = []

        if entries:
            demisto.debug(f"Got entries: {entries} for incident id {remote_args.remote_incident_id}")
            for entry in entries:
                demisto.debug(f"Got entry {entry}")
                if "file" in entry and entry.get("file"):
                    file_entry_content = client.get_file_entry(entry.get("id"))  # type: ignore
                    file_result = fileResult(entry["file"], file_entry_content)

                    formatted_entries.append(file_result)
                else:
                    formatted_entries.append(
                        {
                            "Type": entry.get("type"),
                            "Category": entry.get("category"),
                            "Contents": entry.get("contents"),
                            "ContentsFormat": entry.get("format"),
                            "Tags": entry.get("tags"),  # the list of tags to add to the entry
                            "Note": entry.get("note"),  # boolean, True for Note, False otherwise
                        }
                    )

        # Handle if the incident closed remotely
        if incident.get("status") == IncidentStatus.DONE:
            demisto.debug("incident was closed remotely, adding note")
            formatted_entries.append(
                {
                    "Type": EntryType.NOTE,
                    "Contents": {
                        "dbotIncidentClose": True,
                        "closeReason": incident.get("closeReason"),
                        "closeNotes": incident.get("closeNotes"),
                    },
                    "ContentsFormat": EntryFormat.JSON,
                }
            )

        incident["in_mirror_error"] = ""

        if remote_args.last_update >= modified and not formatted_entries:
            demisto.debug(f"Nothing new in the incident, incident id {remote_args.remote_incident_id}")
            incident = {}  # this empties out the incident, which will result in not updating the local one

        incident["dbotMirrorInstance"] = demisto.integrationInstance()
        incident["id"] = remote_args.remote_incident_id
        # incident['attachment'] = file_attachments
        mirror_data = GetRemoteDataResponse(mirrored_object=incident, entries=formatted_entries)
        return mirror_data

    except Exception as e:
        demisto.error(f"Error in XSOAR incoming mirror for incident {args['id']} \nError message: {e!s}")
        incident = {"id": args["id"], "in_mirror_error": str(e)}

        return GetRemoteDataResponse(mirrored_object=incident, entries=[])


def update_remote_system_command(client: Client, args: dict[str, Any], mirror_tags: set[str]) -> str:
    """update-remote-system command: pushes local changes to the remote system

    :type client: ``Client``
    :param client: XSOAR client to use

    :type args: ``Dict[str, Any]``
    :param args:
        all command arguments, usually passed from ``demisto.args()``.
        ``args['data']`` the data to send to the remote system
        ``args['entries']`` the entries to send to the remote system
        ``args['incidentChanged']`` boolean telling us if the local incident indeed changed or not
        ``args['remoteId']`` the remote incident id

    :type mirror_tags: ``Optional[str]``
    :param mirror_tags:
        The tag that you will mirror out of the incident.

    :return:
        ``str`` containing the remote incident id - really important if the incident is newly created remotely

    :rtype: ``str``
    """
    parsed_args = UpdateRemoteSystemArgs(args)
    if parsed_args.delta:
        demisto.debug(f"Got the following delta keys {list(parsed_args.delta.keys())!s}")

    demisto.debug(f"Sending incident with remote ID [{parsed_args.remote_incident_id}] to remote system\n")

    new_incident_id: str = parsed_args.remote_incident_id  # type: ignore
    updated_incident = {}
    if not parsed_args.remote_incident_id or parsed_args.incident_changed:
        if parsed_args.remote_incident_id:
            # First, get the incident as we need the version
            old_incident = client.get_incident(incident_id=parsed_args.remote_incident_id)
            for changed_key in parsed_args.delta:
                old_incident[changed_key] = parsed_args.delta[changed_key]  # type: ignore
                if changed_key in old_incident.get("CustomFields", {}):
                    old_incident["CustomFields"][changed_key] = parsed_args.delta[changed_key]

            parsed_args.data = old_incident

        else:
            parsed_args.data["createInvestigation"] = True
            # This is used to identify the custom field, so that we will know the source of the incident
            # and will not mirror it in afterwards
            parsed_args.data["CustomFields"] = {"frompong": "true"}

        updated_incident = client.update_incident(incident=parsed_args.data)
        new_incident_id = updated_incident["id"]
        demisto.debug(f"Got back ID [{new_incident_id}]")

    else:
        demisto.debug(
            f"Skipping updating remote incident fields [{parsed_args.remote_incident_id}] as it is not new nor changed."
        )

    if parsed_args.entries:
        for entry in parsed_args.entries:
            demisto.info(f"this is the tag {entry.get('tags', [])}")
            if mirror_tags.intersection(set(entry.get("tags", []))):
                demisto.debug(f'Sending entry {entry.get("id")}')
                client.add_incident_entry(incident_id=new_incident_id, entry=entry)

    # Close incident if relevant
    if updated_incident and parsed_args.inc_status == IncidentStatus.DONE:
        demisto.debug(f"Closing remote incident {new_incident_id}")
        client.close_incident(
            new_incident_id,
            updated_incident.get("version"),  # type: ignore
            parsed_args.data.get("closeReason"),
            parsed_args.data.get("closeNotes"),
        )

    return new_incident_id


def get_and_dedup_incidents(
    client: Client, last_fetched_incidents: list[Any], query: str, max_results: int, last_fetch: Union[str, int]
) -> tuple[list[dict], list[dict], Optional[datetime]]:
    """get incidents and dedup the incidents response.

    Cases:
    1.  Empty incidents list (no new incidents received from API response).
        Return empty list of incidents and the unchanged the list of 'last_fetched_incidents'.

    2.  The response includes incidents from the previous fetch cycle, with the same timestamp.
        Fetch incidents until the number of incidents is equal to the requested max fetch_limit.
        Add the list of fetched incident IDs to the current 'last_fetched_incidents' from last run,
        return a list of new incidents, and updated list of 'last_fetched_incidents'.

    3.  Most recent incident has a later timestamp than other incidents in the response.
        Return a list of new incidents and a list of 'new_ids' containing only the IDs of
        incidents with identical latest times for next run.

    Args:
        incidents (list[dict]): List of incidents from the current fetch response.
        last_fetched_incidents (list[dict]): List of IDs of incidents from last fetch cycle.

    Returns:
        tuple[list[dict], list[str]: The list of dedup incidents and ID list of incidents of current fetch.
    """
    last_fetched_incident_time = dateparser.parse(str(last_fetch))

    new_incidents: list = []
    page = 0
    while len(new_incidents) < max_results:
        incidents = client.search_incidents(
            query=query,
            max_results=max_results,
            start_time=last_fetch,
            field="created",
            page=page,
        )
        demisto.debug(f"incidents: {incidents}")
        # Case 1: Empty response.
        if len(incidents) == 0:
            break
        for incident in incidents:
            if len(new_incidents) >= max_results:
                break
            incident_id = incident.get("id")
            created = incident.get("created", last_fetched_incident_time)
            demisto.debug(f"before incident_creation_time with timezone: {created}")
            incident_creation_time = dateparser.parse(
                incident.get("created", last_fetched_incident_time), settings={"TIMEZONE": "Z"}
            )
            demisto.debug(f"{incident_id} incident_creation_time: {incident_creation_time}")
            if incident_id not in last_fetched_incidents:
                # Case 3: The last fetched incident with the different timestamp then the previous incident.
                if last_fetched_incident_time and incident_creation_time and last_fetched_incident_time < incident_creation_time:
                    demisto.debug(f"XSOAR Mirroring: reset the last_fetched_incidents list with id {incident_id}")
                    last_fetched_incidents = [incident_id]
                # Case 2: The last fetched incident with the same timestamp as the previous incident.
                else:
                    demisto.debug(f"XSOAR Mirroring: attached id {incident_id} to the last_fetched_incidents list")
                    last_fetched_incidents.append(incident_id)
                new_incidents.append(incident)
                last_fetched_incident_time = incident_creation_time
        page += 1
    return new_incidents, last_fetched_incidents, last_fetched_incident_time


def main() -> None:  # pragma: no cover
    params = demisto.params()

    api_key_id, api_key, base_url = validate_and_prepare_basic_params(params)
    verify_certificate = not demisto.params().get("insecure", False)

    # How much time before the first fetch to retrieve incidents
    first_fetch_time = arg_to_datetime(demisto.params().get("first_fetch", "3 days")).strftime(XSOAR_DATE_FORMAT)  # type: ignore

    proxy = demisto.params().get("proxy", False)
    demisto.debug(f"Command being called is {demisto.command()}")
    mirror_tags = set(demisto.params().get("mirror_tag", "").split(",")) if demisto.params().get("mirror_tag") else set()

    query = demisto.params().get("query", "") or ""
    disable_from_same_integration = demisto.params().get("disable_from_same_integration")
    if disable_from_same_integration:
        query += ' -sourceBrand:"XSOAR Mirroring"'

    max_results = arg_to_number(arg=demisto.params().get("max_fetch"), arg_name="max_fetch")
    if not max_results or max_results > MAX_INCIDENTS_TO_FETCH:
        max_results = MAX_INCIDENTS_TO_FETCH

    try:
        headers = {
            "Authorization": api_key,
            "x-xdr-auth-id": api_key_id,
            "Content-Type": "application/json",
        }
        client = Client(base_url=base_url, verify=verify_certificate, headers=headers, proxy=proxy)

        if demisto.command() == "test-module":
            if demisto.params().get("isFetch"):
                fetch_incidents(
                    client=client,
                    max_results=max_results,
                    last_run=demisto.getLastRun(),
                    last_fetch=demisto.getLastRun().get("last_fetch"),
                    first_fetch_time=first_fetch_time,
                    query=query,
                    mirror_direction=demisto.params().get("mirror_direction"),
                    mirror_tag=list(mirror_tags),
                    mirror_playbook_id=demisto.params().get("mirror_playbook_id", True),
                    fetch_incident_history=demisto.params().get("fetch_incident_history", False),
                )

            return_results(test_module(client, first_fetch_time))

        elif demisto.command() == "fetch-incidents":
            next_run, incidents = fetch_incidents(
                client=client,
                max_results=max_results,
                last_run=demisto.getLastRun(),
                last_fetch=demisto.getLastRun().get("last_fetch"),
                first_fetch_time=first_fetch_time,
                query=query,
                mirror_direction=demisto.params().get("mirror_direction"),
                mirror_tag=list(mirror_tags),
                mirror_playbook_id=demisto.params().get("mirror_playbook_id", True),
                fetch_incident_history=demisto.params().get("fetch_incident_history", False),
            )
            demisto.setLastRun(next_run)
            demisto.incidents(incidents)

        elif demisto.command() == "xsoar-search-incidents":
            return_results(search_incidents_command(client, demisto.args()))

        elif demisto.command() == "xsoar-get-incident":
            return_results(get_incident_command(client, demisto.args()))

        elif demisto.command() == "get-mapping-fields":
            return_results(get_mapping_fields_command(client))

        elif demisto.command() == "get-remote-data":
            return_results(get_remote_data_command(client, demisto.args(), demisto.params()))

        elif demisto.command() == "update-remote-system":
            return_results(update_remote_system_command(client, demisto.args(), mirror_tags))

        else:
            raise NotImplementedError("Command not implemented")

    except NotImplementedError:
        raise
    except Exception as e:
        return_error(f"Failed to execute {demisto.command()} command.\nError:\n{e!s}")


""" ENTRY POINT """


if __name__ in ("__main__", "__builtin__", "builtins"):
    main()
