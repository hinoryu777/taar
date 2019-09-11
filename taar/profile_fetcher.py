# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.

from decouple import config
from srgutil.interfaces import IMozLogging
from google.cloud.bigtable import column_family
from google.cloud.bigtable import row_filters
from google.cloud import bigtable
import boto3
import json
import zlib
import datetime

from taar.recommenders import TEST_CLIENT_IDS, EMPTY_TEST_CLIENT_IDS

BIGTABLE_PROJECT_ID = config(
    "BIGTABLE_PROJECT_ID", default="cfr-personalization-experiment"
)
BIGTABLE_INSTANCE_ID = config("BIGTABLE_INSTANCE_ID", default="taar-profile")
BIGTABLE_TABLE_ID = config("BIGTABLE_TABLE_ID", default="test-table")


class BigTableProfileController:
    """
    This class implements the profile database in BigTable
    """

    def __init__(self, ctx, project_id, instance_id, table_id):
        self._ctx = ctx
        self._project_id = project_id
        self._instance_id = instance_id
        self._table_id = table_id
        self._column_family_id = "profile"
        self._column_name = "payload".encode()

        # Define the GC policy to retain only the most recent version
        max_age_rule = column_family.MaxAgeGCRule(datetime.timedelta(days=90))
        max_versions_rule = column_family.MaxVersionsGCRule(1)
        self._gc_rule = column_family.GCRuleUnion(
            rules=[max_age_rule, max_versions_rule]
        )

        self._client = bigtable.Client(project=project_id, admin=False)
        self._instance = self._client.instance(self._instance_id)

    def create_table(self):
        # admin needs to be set to True here so that we can create the
        # table
        admin_client = bigtable.Client(project=self._project_id, admin=True)
        instance = admin_client.instance(self._instance_id)
        print("Creating the {} table.".format(self._table_id))
        table = instance.table(self._table_id)

        column_families = {self._column_family_id: self._gc_rule}
        table.create(column_families=column_families)

    def set_client_profile(self, client_profile):

        # Keys must be UTF8 encoded
        row_key = client_profile["client_id"].encode("utf8")

        table = self._instance.table(self._table_id)

        row = table.direct_row(row_key)
        row.set_cell(
            self._column_family_id,
            self._column_name,
            zlib.compress(json.dumps(client_profile).encode("utf8")),
            timestamp=datetime.datetime.utcnow(),
        )
        table.mutate_rows([row])

    def get_client_profile(self, client_id):
        """This fetches a single client record out of GCP BigTable
        """
        row_key = client_id.encode()

        table = self._instance.table(self._table_id)
        row_filter = row_filters.CellsColumnLimitFilter(1)
        row = table.read_row(row_key, row_filter)
        cell = row.cells[self._column_family_id][self._column_name][0]
        jdata = json.loads(zlib.decompress(cell.value).decode("utf-8"))
        return jdata


class ProfileController:
    """
    This class provides basic read/write access into a AWS DynamoDB
    backed datastore.  The profile controller and profile fetcher code
    should eventually be merged as individually they don't "pull their
    weight".
    """

    def __init__(self, ctx, region_name, table_name):
        """
        Configure access to the DynamoDB instance
        """
        self._ctx = ctx
        self.logger = self._ctx[IMozLogging].get_logger("taar")
        self._ddb = boto3.resource("dynamodb", region_name=region_name)
        self._table = self._ddb.Table(table_name)

    def get_client_profile(self, client_id):
        """This fetches a single client record out of DynamoDB
        """
        try:
            response = self._table.get_item(Key={"client_id": client_id})
            compressed_bytes = response["Item"]["json_payload"].value
            json_byte_data = zlib.decompress(compressed_bytes)
            json_str_data = json_byte_data.decode("utf8")
            return json.loads(json_str_data)
        except KeyError:
            # No client ID found - not really an error
            return None
        except Exception as e:
            # Return None on error.  The caller in ProfileFetcher will
            # handle error logging
            msg = "Error loading client data for {}.  Error: {}"
            self.logger.debug(msg.format(client_id, str(e)))
            return None


class ProfileFetcher:
    """ Fetch the latest information for a client on the backing
    datastore
    """

    def __init__(self, ctx):
        self._ctx = ctx
        self.logger = self._ctx[IMozLogging].get_logger("taar")
        self.__client = None

    @property
    def _client(self):
        if self.__client is None:
            self.__client = BigTableProfileController(
                self._ctx,
                project_id=BIGTABLE_PROJECT_ID,
                instance_id=BIGTABLE_INSTANCE_ID,
                table_id=BIGTABLE_TABLE_ID,
            )
        return self.__client

    def set_client(self, client):
        self.__client = client

    def get(self, client_id):

        if client_id in TEST_CLIENT_IDS or client_id in EMPTY_TEST_CLIENT_IDS:
            return {
                "client_id": client_id,
                "geo_city": "Toronto",
                "subsession_length": 42,
                "locale": "en-CA",
                "os": "Linux",
                "installed_addons": [],
                "disabled_addons_ids": [],
                "bookmark_count": 0,
                "tab_open_count": 0,
                "total_uri": 0,
                "unique_tlds": 0,
            }

        profile_data = self._client.get_client_profile(client_id)

        if profile_data is None:
            self.logger.debug(
                "Client profile not found", extra={"client_id": client_id}
            )
            return None

        addon_ids = [
            addon["addon_id"]
            for addon in profile_data.get("active_addons", [])
            if not addon.get("is_system", False)
        ]

        return {
            "client_id": client_id,
            "geo_city": profile_data.get("city", ""),
            "subsession_length": profile_data.get("subsession_length", 0),
            "locale": profile_data.get("locale", ""),
            "os": profile_data.get("os", ""),
            "installed_addons": addon_ids,
            "disabled_addons_ids": profile_data.get("disabled_addons_ids", []),
            "bookmark_count": profile_data.get("places_bookmarks_count", 0),
            "tab_open_count": profile_data.get(
                "scalar_parent_browser_engagement_tab_open_event_count", 0
            ),
            "total_uri": profile_data.get(
                "scalar_parent_browser_engagement_total_uri_count", 0
            ),
            "unique_tlds": profile_data.get(
                "scalar_parent_browser_engagement_unique_domains_count", 0
            ),
        }
