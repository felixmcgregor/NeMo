# Copyright (c) 2021, NVIDIA CORPORATION.  All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import pytest
from nemo_text_processing.inverse_text_normalization.inverse_normalize import InverseNormalizer
from nemo_text_processing.text_normalization.normalize import Normalizer
from nemo_text_processing.text_normalization.normalize_with_audio import NormalizerWithAudio
from parameterized import parameterized
from utils import PYNINI_AVAILABLE, parse_test_case_file


class TestDate:
    inverse_normalizer_en = InverseNormalizer(lang='en') if PYNINI_AVAILABLE else None

    @parameterized.expand(parse_test_case_file('data_inverse_text_normalization/en/test_cases_date.txt'))
    @pytest.mark.skipif(
        not PYNINI_AVAILABLE, reason="`pynini` not installed, please install via nemo_text_processing/setup.sh"
    )
    @pytest.mark.run_only_on('CPU')
    @pytest.mark.unit
    def test_denorm(self, test_input, expected):
        pred = self.inverse_normalizer_en.inverse_normalize(test_input, verbose=False)
        assert pred == expected

    normalizer_en = Normalizer(input_case='cased', lang='en') if PYNINI_AVAILABLE else None
    normalizer_with_audio_en = NormalizerWithAudio(input_case='cased', lang='en') if PYNINI_AVAILABLE else None

    @parameterized.expand(parse_test_case_file('data_text_normalization/en/test_cases_date.txt'))
    @pytest.mark.skipif(
        not PYNINI_AVAILABLE, reason="`pynini` not installed, please install via nemo_text_processing/setup.sh"
    )
    @pytest.mark.run_only_on('CPU')
    @pytest.mark.unit
    def test_norm_uncased(self, test_input, expected):
        pred = self.normalizer_en.normalize(test_input, verbose=False)
        assert pred == expected
        pred_non_deterministic = self.normalizer_with_audio_en.normalize(test_input, n_tagged=100)
        assert expected in pred_non_deterministic

    normalizer_uppercased = Normalizer(input_case='cased', lang='en') if PYNINI_AVAILABLE else None
    cases_uppercased = {"Aug. 8": "august eighth", "8 Aug.": "the eighth of august", "aug. 8": "august eighth"}

    @parameterized.expand(cases_uppercased.items())
    @pytest.mark.skipif(
        not PYNINI_AVAILABLE, reason="`pynini` not installed, please install via nemo_text_processing/setup.sh"
    )
    @pytest.mark.run_only_on('CPU')
    @pytest.mark.unit
    def test_norm_cased(self, test_input, expected):
        pred = self.normalizer_uppercased.normalize(test_input, verbose=False)
        assert pred == expected
        pred_non_deterministic = self.normalizer_with_audio_en.normalize(test_input, n_tagged=100)
        assert expected in pred_non_deterministic
