#  Copyright 2021 Collate
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#  You may obtain a copy of the License at
#  http://www.apache.org/licenses/LICENSE-2.0
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.
"""NoSQL Sampler"""
from typing import Dict, List, Optional, Tuple

from metadata.generated.schema.entity.data.table import ProfileSampleType, TableData
from metadata.profiler.adaptors.factory import factory
from metadata.profiler.adaptors.nosql_adaptor import NoSQLAdaptor
from metadata.sampler.sampler_interface import SamplerInterface
from metadata.utils.constants import SAMPLE_DATA_DEFAULT_COUNT
from metadata.utils.sqa_like_column import SQALikeColumn


class NoSQLSampler(SamplerInterface):
    """NoSQL generic implementation for the sampler"""

    client: NoSQLAdaptor

    @property
    def table(self):
        return self.entity

    def get_client(self):
        return factory.create(
            self.service_connection_config.__class__.__name__,
            client=self.connection,
        )

    def _rdn_sample_from_user_query(self) -> List[Dict[str, any]]:
        """
        Get random sample from user query
        """
        limit = self._get_limit()
        return self.client.query(
            self.table, self.table.columns, self.sample_query, limit
        )

    def _fetch_sample_data_from_user_query(self) -> TableData:
        """
        Fetch sample data based on a user query. Assuming the enging has one (example: MongoDB)
        If the engine does not support a custom query, an error will be raised.
        """
        records = self._rdn_sample_from_user_query()
        columns = [
            SQALikeColumn(name=column.name.root, type=column.dataType)
            for column in self.table.columns
        ]
        rows, cols = self.transpose_records(records, columns)
        return TableData(
            rows=[list(map(str, row)) for row in rows], columns=[c.name for c in cols]
        )

    def random_sample(self, **__):
        """No randomization for NoSQL"""

    def fetch_sample_data(self, columns: List[SQALikeColumn]) -> TableData:
        if self.sample_query:
            return self._fetch_sample_data_from_user_query()
        return self._fetch_sample_data(columns)

    def _fetch_sample_data(self, columns: List[SQALikeColumn]) -> TableData:
        """
        returns sampled ometa dataframes
        """
        limit = self._get_limit()
        records = self.client.scan(self.table, self.table.columns, int(limit))
        rows, cols = self.transpose_records(records, columns)
        return TableData(
            rows=[list(map(str, row)) for row in rows],
            columns=[col.name for col in cols],
        )

    def _get_limit(self) -> Optional[int]:
        num_rows = self.client.item_count(self.table)
        if self.sample_config.profile_sample_type == ProfileSampleType.PERCENTAGE:
            limit = num_rows * (self.sample_config.profile_sample / 100)
        elif self.sample_config.profile_sample_type == ProfileSampleType.ROWS:
            limit = self.sample_config.profile_sample
        else:
            limit = SAMPLE_DATA_DEFAULT_COUNT
        return limit

    @staticmethod
    def transpose_records(
        records: List[Dict[str, any]], columns: List[SQALikeColumn]
    ) -> Tuple[List[List[any]], List[SQALikeColumn]]:
        rows = []
        for record in records:
            row = []
            for column in columns:
                row.append(record.get(column.name))
            rows.append(row)
        return rows, columns

    def get_columns(self) -> List[Optional[SQALikeColumn]]:
        return [
            SQALikeColumn(name=c.name.root, type=c.dataType) for c in self.table.columns
        ]