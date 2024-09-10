import logging
import requests
import os

from typing import List, Union
from datetime import datetime as _datetime
import json
import geopandas as gpd
from cachetools import TTLCache, cached
from shapely import geometry

from boson.http import serve
from boson.boson_core_pb2 import Property
from boson.conversion import cql2_to_query_params
from geodesic.cql import CQLFilter
from google.protobuf.timestamp_pb2 import Timestamp

logger = logging.getLogger(__name__)
logger.addHandler(logging.StreamHandler())
logger.setLevel(logging.INFO)


STATE_PATH = "app/states.geoparquet"
COUNTIES_PATH = "app/counties.geoparquet"


class Boundaries:
    def __init__(self, path: str):
        self.df = gpd.read_parquet(path)

    def intersects(self, geom) -> gpd.GeoDataFrame:
        idx = self.df.intersects(geom)
        return self.df.copy().loc[idx]


counties = Boundaries(COUNTIES_PATH)
states = Boundaries(STATE_PATH)


class APIWrapperRemoteProvider:
    def __init__(self) -> None:
        self.api_url = "https://quickstats.nass.usda.gov/api/api_GET/"
        self.max_page_size = 50000
        # FIXME: take API key out of provider code
        self.api_default_params = {
            "key": "6F441079-980F-3F40-BE4B-F5F17B7ABED3",
        }

    def get_counties_from_geometry(self, geom) -> gpd.GeoDataFrame:
        """
        Given a geometry or bbox, return a geodataframe with 'county_name', 'state_name', 'geometry' (county geometry), sorted by 'COUNTYNS'
        County_name and state_name are the index

        input:
        geom - shapely.geometry or bbox

        output:
        counties_df - geopandas.GeoDataFrame
        """

        if isinstance(geom, list) or isinstance(geom, tuple):
            if len(geom) != 4:
                raise ValueError("bbox must be a bounding box with 4 coordinates")
            geom = geometry.box(*geom)

        elif not isinstance(geom, geometry.base.BaseGeometry):
            raise ValueError("geom must be a shapely geometry or a bbox")

        # get the counties that intersect with the geometry
        counties_df = counties.intersects(geom)
        if len(counties_df) == 0:
            return gpd.GeoDataFrame(columns=["geometry", "id"])

        # get the states that intersect with the geometry, and drop all except the statefp and name
        states_df = states.intersects(geom)
        states_df = states_df[["STATEFP", "NAME"]]

        # strip the whitespace from the statefp
        counties_df["STATEFP"] = counties_df["STATEFP"].str.strip()
        states_df["STATEFP"] = states_df["STATEFP"].str.strip()

        # join the counties and states on the statefp
        counties_df.set_index("STATEFP", inplace=True)
        states_df.set_index("STATEFP", inplace=True)
        counties_and_states = counties_df.join(states_df, rsuffix="_state")

        counties_and_states.rename(columns={"NAME": "county_name", "NAME_state": "state_name"}, inplace=True)
        counties_and_states = counties_and_states.sort_values(by=["COUNTYNS"])

        counties_gdf = counties_and_states.set_index(["county_name", "state_name"])
        counties_gdf = counties_gdf[["geometry", "COUNTYNS"]]

        return counties_gdf

    def get_states_from_geometry(self, geom) -> gpd.GeoDataFrame:
        """
        do this later (for when we can only search by state)
        """
        pass

    def create_query_list(
        self,
        bbox: List[float] = [],
        datetime: List[_datetime] = [],
        intersects: object = None,
        # collections: List[str] = [],
        # feature_ids: List[str] = [],
        filter: Union[CQLFilter, dict] = None,
        # fields: Union[List[str], dict] = None,
        # sortby: dict = None,
        method: str = "POST",
        # page: int = None,
        # page_size: int = None,
        **kwargs,
    ) -> List[dict]:
        """
        This parses the geodesic search parameters and outputs a list of parameter dicts, one for each state or county and year
        """
        api_params = {}

        """
        DEFAULTS
        """
        if self.api_default_params:
            api_params.update(self.api_default_params)

        """
        BBOX/INTERSECTS::
        bbox must be translated into a list of county names (or state names)
        """
        if bbox:
            logger.info(f"Input bbox: {bbox}")
            geom = geometry.box(*bbox)

        elif intersects:
            logger.info(f"Input intersects: {intersects}")
            geom = intersects

        else:
            logger.info("No bbox or intersects provided. Using US as default.")
            geom = geometry.box(-179.9, 18.0, -66.9, 71.4)

        counties_gdf = self.get_counties_from_geometry(geom)

        """
        DATETIME: Produce a list of years that intersect with the datetime range 
        """
        if datetime:
            logger.info(f"Received datetime: {datetime}")

            start_year = datetime[0].year
            end_year = datetime[1].year

            years_range = list(range(start_year, end_year + 1))

        """
        FILTER:
        convert cql filter to query parameters and update
        """
        if filter:
            logger.info(f"Received CQL filter")
            filter_params = cql2_to_query_params(filter)

        query_list = []

        for row_index, row in counties_gdf.reset_index().iterrows():
            query_params = {}

            query_params["county_name"] = row["county_name"]
            query_params["state_name"] = row["state_name"]
            query_params["sector"] = "CROPS"

            if filter:
                # FIXME: make sure this doesn't overwrite the other params, and that it consists only of valid params
                query_params.update(filter_params)

            for year_index, year in enumerate(years_range):
                query_params["year"] = year
                query_params["query_index"] = row_index * len(years_range) + year_index
                query_list.append(query_params)

        return query_list

    def convert_results_to_gdf(self, response: Union[dict, List[dict]]) -> gpd.GeoDataFrame:
        """
        Convert the response from the API to a GeoDataFrame. We are assuming the response is a list of json/dict.
        You may need to get the "results" key from the response, depending on the API.

        The template assumes point features and a single datetime, but this can be modified to handle other geometries
        and multiple datetimes. The remaining outputs from the API response can be added to the properties dictionary.
        """

        # This may need editing, depending on the API response
        if isinstance(response, dict):
            response = response.get("results", [])

        logger.info("Converting API response to GeoDataFrame.")
        logger.info(f"Received {len(response)} results. Converting to GeoDataFrame.")
        if len(response) == 0:
            return gpd.GeoDataFrame(columns=["geometry", "id"])

        logger.info(f"First result: {response[0]}")

        # TODO: Update the keys to match the API response
        LATIDUDE_KEY = "Latitude"
        LONGITUDE_KEY = "Longitude"
        ID_KEY = "id"
        DATETIME_KEY = "UTC"

        gdf = gpd.GeoDataFrame(
            response,
            geometry=gpd.points_from_xy(
                [obs.get(LONGITUDE_KEY) for obs in response],
                [obs.get(LATIDUDE_KEY) for obs in response],
            ),
        )

        gdf.set_index(ID_KEY, inplace=True)

        # TODO: update datetime format
        gdf["datetime"] = gdf[DATETIME_KEY].apply(lambda x: _datetime.strptime(x, format="%Y-%m-%dT%H:%M")).astype(str)

        return gdf

    def request_features(self, **kwargs) -> gpd.GeoDataFrame:
        """
        Request data from the API and return a GeoDataFrame. This function is unlikely to need
        modification.
        """
        # Translate the input parameters to API parameters
        logger.info(f"Parsing search input parameters: {kwargs}")
        api_params = self.parse_input_params(**kwargs)

        # Make a GET request to the API
        logger.info(f"Making request with params: {api_params}")
        response = requests.get(self.api_url, api_params)

        # Check if the request was successful (status code 200)
        if response.status_code == 200:
            # Parse and use the response data (JSON in this case)
            res = response.json()

            # Check if the response is empty
            if not res:
                logger.info("No results returned from API")
                gdf = gpd.GeoDataFrame(columns=["geometry", "id"])

            gdf = self.convert_results_to_gdf(res)
            logger.info(f"Received {len(gdf)} features")
        else:
            logging.error(f"Error: {response.status_code}")
            gdf = gpd.GeoDataFrame(columns=["geometry", "id"])

        return gdf

    def search(self, pagination={}, provider_properties={}, **kwargs) -> gpd.GeoDataFrame:
        """Implements the Boson Search endpoint."""
        logger.info("Making request to API.")
        logger.info(f"Search received kwargs: {kwargs}")

        """
        PAGINATION and LIMIT: if limit is None, Boson will page through all results. Set a max
        page size in the __init__ to control the size of each page. If limit is set, the search function
        will return that number of results. Pagination is a dictionary with the keys "page" and "page_size".
        We will pass "page" and "page_size" to the request_features function.
        """
        page = 1
        page_size = self.max_page_size
        limit = kwargs.get("limit", None)
        if limit == 0:
            limit = None
        if limit is not None:
            page_size = limit if limit <= self.max_page_size else self.max_page_size

        if pagination:
            logger.info(f"Received pagination: {pagination}")
            page = pagination.get("page", None)
            page_size = pagination.get("page_size", self.max_page_size)

        """
        PROVIDER_PROPERTIES: 
        """
        if provider_properties:
            logger.info(f"Received provider_properties from boson_config.properties: {provider_properties}")
            # Check for source_desc (Program)
            source_desc = provider_properties.get("source_desc", "SURVEY")
            kwargs["source_desc"] = source_desc

            # Check for statisticcat_desc (Statistic Category)
            statisticcat_desc = provider_properties.get("statisticcat_desc", None)
            if statisticcat_desc:
                kwargs["statisticcat_desc"] = statisticcat_desc

        gdf = self.request_features(page=page, page_size=page_size, **kwargs)

        return gdf, {
            "page": page + 1,
            "page_size": page_size,
        }

    def get_queryables_from_openapi(self, openapi_path: str) -> dict:
        """
        This method is used to automatically generate the queryables from an openapi file. Manually entering the
        queryyables is laborious. If the external API provides and OpenAPI spec, this method will read it from
        a json file and return the queryables automatically. (credit: Mark Schulist)
        """
        with open(openapi_path, "r") as f:  # loading locally because more speedy
            response = json.load(f)
        queryables = {}

        path = "/occurrence/search"  # TODO: Update with path for your API

        params = response["paths"][path]["get"]["parameters"]

        for param in params:
            title = param.get("name")
            type = param.get("type")
            enum = None
            if param.get("schema") is not None:
                schema = param.get("schema")
                if schema.get("items") is not None:
                    items = schema.get("items")
                    enum = items.get("enum")
            if enum is not None:
                queryables[title] = Property(title=title, type=type, enum=enum)
            else:
                queryables[title] = Property(title=title, type=type)

        return queryables

    def queryables(self, **kwargs) -> dict:
        """
        Update this method to return a dictionary of queryable parameters that the API accepts.
        The keys should be the parameter names. The values should be a Property object that follows
        the conventions of JSON Schema.
        """
        # if you have an openapi file, you can use the get_queryables_from_openapi method
        # to automatically generate the queryables
        if os.path.isfile("path_to_openapi_file"):
            return self.get_queryables_from_openapi(openapi_path="path_to_openapi_file")
        else:
            return {
                "example_parameter": Property(
                    title="parameter_title",
                    type="string",
                    enum=[
                        "option1",
                        "option2",
                        "option3",
                    ],
                ),
                "example_parameter2": Property(
                    title="parameter_title2",
                    type="integer",
                ),
                "example_parameter3": Property(
                    title="parameter_title3",
                    type="integer",
                ),
                "example_parameter4": Property(
                    title="parameter_title4",
                    type="boolean",
                ),
            }


api_wrapper = APIWrapperRemoteProvider()
app = serve(search_func=api_wrapper.search, queryables_func=api_wrapper.queryables)
