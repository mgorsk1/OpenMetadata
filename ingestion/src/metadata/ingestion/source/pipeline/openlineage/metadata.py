# metadata.py

"""
OpenLineage source to extract metadata from Kafka events
"""
import itertools
import json
import logging
import traceback
from functools import reduce
from typing import Any, Dict, Iterable, List, Optional, Tuple

from metadata.generated.schema.api.data.createPipeline import CreatePipelineRequest
from metadata.generated.schema.api.data.createTable import CreateTableRequest
from metadata.generated.schema.api.lineage.addLineage import AddLineageRequest
from metadata.generated.schema.entity.data.table import Column, Table
from metadata.generated.schema.entity.services.connections.pipeline.openLineageConnection import (
    OpenLineageConnection,
)
from metadata.generated.schema.metadataIngestion.workflow import (
    Source as WorkflowSource,
)
from metadata.generated.schema.type.entityLineage import (
    ColumnLineage,
    EntitiesEdge,
    LineageDetails,
    Source,
)
from metadata.generated.schema.type.entityReference import EntityReference
from metadata.ingestion.api.models import Either, StackTraceError
from metadata.ingestion.api.steps import InvalidSourceException
from metadata.ingestion.models.pipeline_status import OMetaPipelineStatus
from metadata.ingestion.ometa.ometa_api import OpenMetadata
from metadata.ingestion.source.pipeline.openlineage.models import (
    EventType,
    LineageEdge,
    LineageNode,
    OpenLineageEvent,
    TableDetails,
    TableFQN,
)
from metadata.ingestion.source.pipeline.openlineage.utils import (
    message_to_open_lineage_event,
)
from metadata.ingestion.source.pipeline.pipeline_service import PipelineServiceSource


class OpenlineageSource(PipelineServiceSource):
    """
    Implements the necessary methods of PipelineServiceSource to facilitate registering OpenLineage pipelines with
    metadata into Open Metadata.

    Works under the assumption that OpenLineage integrations produce events to Kafka topic, which is a source of events
    for this connector.

    Only 'SUCCESS' OpenLineage events are taken into account in this connector.

    Configuring OpenLineage integrations: https://openlineage.io/docs/integrations/about
    """

    @classmethod
    def create(cls, config_dict, metadata: OpenMetadata):
        """Create class instance"""
        config: WorkflowSource = WorkflowSource.parse_obj(config_dict)
        connection: OpenLineageConnection = config.serviceConnection.__root__.config
        if not isinstance(connection, OpenLineageConnection):
            raise InvalidSourceException(
                f"Expected OpenLineageConnection, but got {connection}"
            )
        return cls(config, metadata)

    def prepare(self):
        """Nothing to prepare"""

    def close(self) -> None:
        self.metadata.close()

    def _run_om_search_query(
        self, es_index: str, field_value: str, field_name: str = "fullyQualifiedName"
    ) -> Optional[List[Dict]]:
        """
        Conduct search directly on search index used by OpenMetadata. Search will be conducted on field_name field.

        :param es_index: Elasticsearch index.
        :param field_value: Value we are looking for.
        :param field_name: Field (from es index mapping) in which we are looking for field_value.
        :return: list (possibly empty) of search results from hits.hits object.
        """
        try:
            data = self.metadata.client.get(
                "/search/query",
                data={"q": f"{field_name}:{field_value}", "index": es_index},
            )

            return data["hits"]["hits"]
        except Exception:
            logging.warning(
                f"""
                Error fetching search query results. Index: [{es_index}] | FieldName: [{field_name}] 
                | FieldValue: [{field_value}] """,
                exc_info=True,
            )
            return None

    def _get_om_document(
        self, es_index: str, field_value: str, field_name: str = "fullyQualifiedName"
    ) -> Optional[Dict]:
        """
        Return first document/object from search results. Collects data from _source object (es search result).

        :param es_index: Elasticsearch index.
        :param field_value: Value we are looking for.
        :param field_name: Field (from es index mapping) in which we are looking for field_value.
        :return: Single object (if present) from search query results.
        """
        try:
            results = self._run_om_search_query(es_index, field_value, field_name) or []

            return results[0]["_source"]
        except (KeyError, IndexError, ValueError):
            logging.warning(
                f"Document with value [{field_value}] in [{field_name}] field not found within [{es_index}] index."
            )
            return None

    def _get_entity_field_from_partial_name(
        self, partial_name: str, field: str, index: str = "table_search_index"
    ) -> Optional[Any]:
        """
        Searches for document in OpenMetadata search index based on partial field value. Search is conducted across
        database services configured within metadata ingestion instance.

        :param partial_name: partial name of entity we are looking for
        :param field: field against which partial_name will be compared.
        :param index: es Index on which search will be conducted.
        :return: if document exists, return value of desired field. Otherwise, return None.
        """
        for db_service_fqn in self.source_config.dbServiceNames:
            # OL doesn't provide database info (only schema and table name) since we conduct wildcard search to find
            # matching fqn in table search index
            wildcard_fqn = f"{db_service_fqn}.*.{partial_name}"
            data = self._get_om_document(index, wildcard_fqn, field)

            if data:
                try:
                    result = reduce(lambda x, y: x[y], field.split("."), data)

                    return result
                except KeyError:
                    raise ValueError(f"{field} field not found in retrieved document")

            return None

    @classmethod
    def _get_table_name(cls, data: Dict) -> TableDetails:
        """
        extracts table entity partial name from input/output entry collected from Open Lineage.

        :param data: single entry from inputs/outputs objects
        :return: partial_schema, partial_table
        """
        symlinks = data.get("facets", {}).get("symlinks", {}).get("identifiers", [])

        # for some OL events name can be extracted from dataset facet but symlinks is preferred so - if present - we
        # use it instead
        if len(symlinks) > 0:
            try:
                # @todo verify if table can have multiple identifiers pointing at it
                name_parts = symlinks[0]["name"].split(".")
            except (KeyError, IndexError):
                raise ValueError(
                    "input table name cannot be retrieved from symlinks.identifiers facet."
                )
        else:
            try:
                name_parts = data["name"].split(".")
            except KeyError:
                raise ValueError(
                    "input table name cannot be retrieved from name attribute."
                )

        # we take last two elements to explicitly collect schema and table names
        # in BigQuery Open Lineage events name_parts would be list of 3 elements as first one is GCP Project ID
        # however, concept of GCP Project ID is not represented in Open Metadata and hence - we need to skip this part
        return TableDetails(name=name_parts[-1], schema=name_parts[-2])

    def _get_table_fqn(self, partial_schema, partial_table: str) -> str:
        """
        Based on partial schema and table names look for matching table object in open metadata.

        For example - for partial schema 'demo' and partial_table 'transactions', a partial match search on
        *.demo.partial_table fullyQualifiedName will be conducted across all database_services configured in ingestion.

        :param partial_schema: schema name (not fullyQualifiedName of schema, hence 'partial')
        :param partial_table: table name (not fullyQualifiedName of table, hence 'partial')
        :return: fully qualified name of a table in Open Metadata
        """
        partial_name = f"{partial_schema}.{partial_table}"
        return self._get_entity_field_from_partial_name(
            partial_name, "fullyQualifiedName"
        )

    def _get_schema_fqn(self, partial_schema: str) -> str:
        """
        Based on partial schema name look for any matching table object in open metadata.

        For example - for partial schema 'demo', a partial match search on *.demo databaseSchema.fullyQualifiedName
        will be conducted across all database_services configured in ingestion.

        :param partial_schema: schema name (not fullyQualifiedName of schema, hence 'partial')
        :return: fully qualified name of a schema in Open Metadata
        """
        result = self._get_entity_field_from_partial_name(
            partial_schema, "databaseSchema.fullyQualifiedName"
        )
        return result

    @classmethod
    def _render_pipeline_name(cls, pipeline_details: OpenLineageEvent) -> str:
        """
        Renders pipeline name from parent facet of run facet. It is our expectation that every OL event contains parent
        run facet so we can always create pipeline entities and link them to lineage events.

        :param run_facet: Open Lineage run facet
        :return: pipeline name (not fully qualified name)
        """
        run_facet = pipeline_details.run_facet

        namespace = run_facet["facets"]["parent"]["job"]["namespace"]
        name = run_facet["facets"]["parent"]["job"]["name"]

        return f"{namespace}-{name}"

    @classmethod
    def _filter_event_by_type(
        cls, event: OpenLineageEvent, event_type: EventType
    ) -> Optional[Dict]:
        """
        returns event if it's of particular event_type.
        for example - for lineage events we will be only looking for EventType.COMPLETE event type.

        :param event: Open Lineage raw event.
        :param event_type: type of event we are looking for.
        :return: Open Lineage event if matches event_type, otherwise None
        """
        return event if event.event_type == event_type else {}

    @classmethod
    def _get_om_table_columns(cls, table_input: Dict) -> Optional[List]:
        """

        :param table_input:
        :return:
        """
        try:
            fields = table_input["facets"]["schema"]["fields"]

            # @todo check if this way of passing type is ok
            columns = [
                Column(name=f.get("name"), dataType=f.get("type").upper())
                for f in fields
            ]
            return columns
        except KeyError:
            return None

    def get_create_table_request(
        self, table: Dict
    ) -> Tuple[Optional[Either], Optional[str]]:
        """
        If certain table from Open Lineage events doesn't already exist in Open Metadata, register appropriate entity.
        This makes sense especially for output facet of OpenLineage event - as database service ingestion is a scheduled
        process we might fall into situation where we received Open Lineage event about creation of a table that is yet
        to be ingested by database service ingestion process. To avoid missing on such lineage scenarios, we will create
        table entity beforehand.

        :param table: single object from inputs/outputs facet
        :return: request to create the entity (if needed) and it's fully qualified name (if possible)
        """
        try:
            table_details = OpenlineageSource._get_table_name(table)
        except ValueError as e:
            return (
                Either(
                    left=StackTraceError(
                        name="",
                        error=f"Failed to get partial table name: {e}",
                        stack_trace=traceback.format_exc(),
                    )
                ),
                None,
            )

        try:
            om_table_fqn = self._get_table_fqn(table_details.schema, table_details.name)
        except KeyError as e:
            return (
                Either(
                    left=StackTraceError(
                        name="",
                        error=f"Failed to get fully qualified table name: {e}",
                        stack_trace=traceback.format_exc(),
                    )
                ),
                None,
            )

        # If OM Table FQN was not found based on OL Partial Name - we need to register it.
        if not om_table_fqn:
            try:
                om_schema_fqn = self._get_schema_fqn(table_details.schema)
            except KeyError as e:
                return (
                    Either(
                        left=StackTraceError(
                            name="",
                            error=f"Failed to get fully qualified schema name: {e}",
                            stack_trace=traceback.format_exc(),
                        )
                    ),
                    None,
                )

            # After finding schema fqn (based on partial schema name) we know where we can create table
            # and we move forward with creating request.
            if om_schema_fqn:
                columns = OpenlineageSource._get_om_table_columns(table) or []

                request = CreateTableRequest(
                    name=table_details.name,
                    columns=columns,
                    databaseSchema=om_schema_fqn,
                )
                # since request is yielded we consider table to be created and update state, so it's not created again

                om_table_fqn = f"{om_schema_fqn}.{table_details.name}"

                return Either(right=request), om_table_fqn
        else:
            return None, om_table_fqn

        return None, None

    @classmethod
    def _get_ol_table_name(cls, table: Dict) -> str:
        return "/".join(table.get(f) for f in ["namespace", "name"]).replace("//", "/")

    def _get_column_lineage(
        self, inputs: List, outputs: List
    ) -> Dict[str, List[ColumnLineage]]:
        _result: Dict[
            str, Dict[str, List]
        ] = {}  # key - output table fqn, value dict where k - output column fqn,
        # v - list of input columns (fqns)
        result: Dict[str, List[ColumnLineage]] = {}

        ol_name_to_fqn_map = {
            OpenlineageSource._get_ol_table_name(table): self._get_table_fqn(
                OpenlineageSource._get_table_name(table).schema,
                OpenlineageSource._get_table_name(table).name,
            )
            for table in inputs + outputs
        }

        for table in outputs:
            _, output_table_fqn = self.get_create_table_request(table)[1]

            for field_name, field_spec in (
                table.get("facets", {})
                .get("columnLineage", {})
                .get("fields", {})
                .items()
            ):
                for input_field in field_spec.get("inputFields", []):
                    input_table_ol_name = OpenlineageSource._get_ol_table_name(
                        input_field
                    )

                    output_column_fqn = f"{output_table_fqn}.{field_name}"

                    if output_table_fqn not in _result:
                        _result[output_table_fqn] = {}

                    if output_column_fqn not in _result[output_table_fqn]:
                        _result[output_table_fqn][output_column_fqn] = []

                    _result[output_table_fqn][output_column_fqn].append(
                        f'{ol_name_to_fqn_map.get(input_table_ol_name)}.{input_field.get("field")}'  # input column
                    )

        for output_table, output_columns in _result.items():
            result[output_table] = [
                ColumnLineage(toColumn=output_column, fromColumns=from_columns)
                for output_column, from_columns in output_columns.items()
            ]

        return result

    def yield_pipeline(
        self, pipeline_details: OpenLineageEvent
    ) -> Iterable[Either[CreatePipelineRequest]]:
        pipeline_name = self.get_pipeline_name(pipeline_details)
        try:
            description = f"""```json
            {json.dumps(pipeline_details.run_facet, indent=4).strip()}
                ```"""
            request = CreatePipelineRequest(
                name=pipeline_name,
                service=self.context.pipeline_service.fullyQualifiedName.__root__,
                description=description,
            )

            yield Either(right=request)
            self.register_record(pipeline_request=request)
        except ValueError:
            yield Either(
                left=StackTraceError(
                    name=pipeline_name,
                    message="Failed to collect metadata required for pipeline creation.",
                ),
                stack_trace=traceback.format_exc(),
            )

    def yield_pipeline_lineage_details(
        self, pipeline_details: OpenLineageEvent
    ) -> Iterable[Either[AddLineageRequest]]:
        inputs, outputs = pipeline_details.inputs, pipeline_details.outputs

        input_edges: List[LineageNode] = []
        output_edges: List[LineageNode] = []

        for spec in [(inputs, input_edges), (outputs, output_edges)]:
            tables, tables_list = spec

            for table in tables:
                create_table_request, table_fqn = self.get_create_table_request(table)

                if create_table_request:
                    yield create_table_request

                if table_fqn:
                    tables_list.append(
                        LineageNode(
                            fqn=TableFQN(value=table_fqn),
                            uuid=self.metadata.get_by_name(Table, table_fqn).id,
                        )
                    )

        edges = [
            LineageEdge(from_node=n[0], to_node=n[1])
            for n in itertools.product(input_edges, output_edges)
        ]

        column_lineage = self._get_column_lineage(inputs, outputs)

        for edge in edges:
            yield Either(
                right=AddLineageRequest(
                    edge=EntitiesEdge(
                        fromEntity=EntityReference(
                            id=edge.from_node.uuid, type=edge.from_node.node_type
                        ),
                        toEntity=EntityReference(
                            id=edge.to_node.uuid, type=edge.to_node.node_type
                        ),
                        lineageDetails=LineageDetails(
                            pipeline=EntityReference(
                                id=self.context.pipeline.id.__root__,
                                type="pipeline",
                            ),
                            source=Source.OpenLineage,  # pylint: disable=no-member
                            columnsLineage=column_lineage.get(
                                edge.to_node.fqn.value, []
                            ),
                        ),
                    ),
                )
            )

    def get_pipelines_list(self) -> Optional[List[Any]]:
        """Get List of all pipelines"""
        try:
            consumer = self.client
            session_active = True
            empty_msg_cnt = 0
            pool_timeout = self.service_connection.poolTimeout
            while session_active:
                message = consumer.poll(timeout=pool_timeout)
                if message is None:
                    empty_msg_cnt += 1
                    if (
                        empty_msg_cnt * pool_timeout
                        > self.service_connection.sessionTimeout
                    ):
                        # There is no new messages, timeout is passed
                        session_active = False
                else:
                    empty_msg_cnt = 0
                    try:
                        _result = message_to_open_lineage_event(
                            json.loads(message.value())
                        )
                        result = self._filter_event_by_type(_result, EventType.COMPLETE)
                        yield result
                    except Exception:
                        pass

        except Exception as e:
            traceback.print_exc()

            raise InvalidSourceException(f"Failed to read from Kafka: {str(e)}")

        finally:
            # Close down consumer to commit final offsets.
            # @todo address this
            consumer.close()

    def get_pipeline_name(self, pipeline_details: OpenLineageEvent) -> str:
        return OpenlineageSource._render_pipeline_name(pipeline_details)

    def yield_pipeline_status(
        self, pipeline_details: OpenLineageEvent
    ) -> Iterable[Either[OMetaPipelineStatus]]:
        pass
