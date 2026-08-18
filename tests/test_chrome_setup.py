import importlib.util
import csv
import io
import json
import os
import pathlib
import re
import subprocess
import sys
import unittest
from contextlib import redirect_stdout
from unittest import mock


SCRIPT_PATH = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "boss_cdp_raw.py"


def load_module():
    sys.modules.setdefault("websocket", mock.Mock())
    sys.modules.setdefault("requests", mock.Mock())
    spec = importlib.util.spec_from_file_location("boss_cdp_raw", SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class ChromeSetupTests(unittest.TestCase):
    def test_default_cdp_profile_is_persistent_and_not_default_or_tmp(self):
        module = load_module()

        self.assertNotEqual(module.DEFAULT_CDP_DATA_DIR, module.DEFAULT_PROFILE_DIR)
        self.assertNotIn("/tmp/", module.DEFAULT_CDP_DATA_DIR)
        self.assertTrue(module.DEFAULT_CDP_DATA_DIR.endswith(".boss-zhipin-scraper/chrome-profile"))

    def test_default_result_dir_is_persistent_user_state(self):
        module = load_module()

        self.assertNotIn("/tmp/", module.DEFAULT_RESULT_DIR)
        self.assertTrue(module.DEFAULT_RESULT_DIR.endswith(".boss-zhipin-scraper/job-result"))
        self.assertTrue(module.default_output_path("jobs").startswith(module.DEFAULT_RESULT_DIR))
        self.assertTrue(module.default_output_path("details").startswith(module.DEFAULT_RESULT_DIR))
        self.assertIn("boss_jobs_", module.default_output_path("jobs"))
        self.assertIn("boss_details_", module.default_output_path("details"))

    def test_create_page_session_uses_normal_foreground_target_without_script_injection(self):
        module = load_module()
        cdp = mock.Mock()
        cdp.send.side_effect = [
            {"result": {"targetId": "target-1"}},
            {"result": {"sessionId": "session-1"}},
        ]

        result = module.create_page_session(cdp)

        self.assertEqual(result, ("target-1", "session-1"))
        self.assertEqual(
            cdp.send.call_args_list,
            [
                mock.call(
                    "Target.createTarget",
                    {"url": "about:blank", "background": False},
                ),
                mock.call(
                    "Target.attachToTarget",
                    {"targetId": "target-1", "flatten": True},
                ),
            ],
        )

    def test_create_page_session_can_open_interactive_foreground_target(self):
        module = load_module()
        cdp = mock.Mock()
        cdp.send.side_effect = [
            {"result": {"targetId": "login-target"}},
            {"result": {"sessionId": "login-session"}},
        ]

        result = module.create_page_session(
            cdp,
            background=False,
        )

        self.assertEqual(result, ("login-target", "login-session"))
        self.assertEqual(
            cdp.send.call_args_list,
            [
                mock.call(
                    "Target.createTarget",
                    {
                        "url": "about:blank",
                        "background": False,
                    },
                ),
                mock.call(
                    "Target.attachToTarget",
                    {"targetId": "login-target", "flatten": True},
                ),
            ],
        )

    def test_default_city_is_shanghai_when_not_provided(self):
        module = load_module()

        self.assertEqual(module.DEFAULT_CITY_INPUT, "上海")
        self.assertEqual(module.resolve_city(module.DEFAULT_CITY_INPUT), ("上海", "101020100"))

    # ----- 本地静态城市码表（data/city_codes.json，见 issue #24）-----

    def test_local_city_map_loads_and_valid(self):
        """本地码表能加载、是字典、非空、value 全是数字字符串。"""
        module = load_module()
        name_to_code, code_to_name = module.load_local_city_map()

        self.assertIsInstance(name_to_code, dict)
        self.assertGreater(len(name_to_code), 100, "码表应包含上百个城市")
        for name, code in name_to_code.items():
            self.assertIsInstance(name, str)
            self.assertIsInstance(code, str)
            self.assertTrue(code.isdigit(), f"城市码应为数字字符串: {name}={code!r}")
        # 反向映射一致
        self.assertEqual(code_to_name.get("101020100"), "上海")

    def test_local_city_map_contains_known_cities(self):
        """码表覆盖一线城市 + 三/四线城市（验证是全量，非旧 24 城）。"""
        module = load_module()
        name_to_code, _ = module.load_local_city_map()

        for city in ("全国", "北京", "上海", "深圳"):
            self.assertIn(city, name_to_code, f"缺少常见城市: {city}")
        # 三/四线城市（旧内置码表没有的），证明已扩展到全量
        for tier34 in ("赣州", "洛阳", "临沂", "襄阳"):
            self.assertIn(tier34, name_to_code, f"缺少三四线城市: {tier34}")

    def test_local_city_map_is_superset_of_old_builtin(self):
        """防回归：新静态码表必须 ⊇ 原内置 24 城且码值一致。"""
        module = load_module()
        name_to_code, _ = module.load_local_city_map()

        old_builtin = {
            "全国": "100010000",
            "北京": "101010100", "上海": "101020100", "广州": "101280100",
            "深圳": "101280600", "杭州": "101210100", "成都": "101270100",
            "西安": "101110100", "重庆": "101040100", "南京": "101190100",
            "长沙": "101250100", "福州": "101230100", "武汉": "101200100",
            "合肥": "101220100", "济南": "101120100", "大连": "101070200",
            "青岛": "101120200", "宁波": "101210400", "厦门": "101230200",
            "天津": "101030100", "苏州": "101190400", "郑州": "101180100",
            "东莞": "101281600", "佛山": "101280800", "沈阳": "101070100",
        }
        for name, code in old_builtin.items():
            self.assertEqual(name_to_code.get(name), code,
                             f"原内置城市 {name}={code} 在新码表中缺失或码值不一致")

    # ----- resolve_city 三级查询链 -----

    def test_resolve_city_hit_local_map(self):
        """本地静态码表命中（含三四线城市）。"""
        module = load_module()

        for name, code in [("上海", "101020100"), ("赣州", "101240700")]:
            self.assertEqual(module.resolve_city(name), (name, code))

    def test_resolve_city_reverse_lookup(self):
        """用城市码反查中文名。"""
        module = load_module()

        self.assertEqual(module.resolve_city("101020100"), ("上海", "101020100"))
        self.assertEqual(module.resolve_city("101240700"), ("赣州", "101240700"))

    def test_resolve_city_fallback_to_live(self):
        """本地码表没有时降级到运行时拉取（mock）。"""
        module = load_module()

        with mock.patch.object(module, "load_local_city_map",
                               return_value=({}, {})), \
             mock.patch.object(module, "load_live_city_maps",
                               return_value=({"长春": "101060100"},
                                             {"101060100": "长春"})):
            self.assertEqual(module.resolve_city("长春"), ("长春", "101060100"))
            self.assertEqual(module.resolve_city("101060100"), ("长春", "101060100"))

    def test_resolve_city_fallback_to_raw(self):
        """正反向映射均未命中时，仍接受 9 位裸 city code。"""
        module = load_module()

        with mock.patch.object(module, "load_local_city_map",
                               return_value=({}, {})) as local_loader, \
             mock.patch.object(module, "load_live_city_maps",
                               return_value=({}, {})) as live_loader:
            self.assertEqual(module.resolve_city("999999999"), ("999999999", "999999999"))
        local_loader.assert_called_once_with()
        live_loader.assert_called_once_with()

    def test_resolve_city_rejects_unknown_chinese_city(self):
        """未知中文城市不能原样作为 city 参数继续抓取。"""
        module = load_module()

        with mock.patch.object(module, "load_local_city_map",
                               return_value=({}, {})), \
             mock.patch.object(module, "load_live_city_maps",
                               return_value=({}, {})):
            with self.assertRaisesRegex(module.CityResolutionError,
                                        "无法解析城市 '不存在市'"):
                module.resolve_city("不存在市")

    def test_resolve_city_rejects_when_local_map_missing_and_live_api_fails(self):
        """本地码表缺失且在线接口失败时明确报错。"""
        module = load_module()

        with mock.patch.object(module, "load_local_city_map",
                               return_value=({}, {})), \
             mock.patch.object(module, "fetch_boss_json",
                               side_effect=OSError("network unavailable")):
            with self.assertLogs(module.log, level="WARNING") as logs:
                with self.assertRaises(module.CityResolutionError):
                    module.resolve_city("长春")
        self.assertIn("加载 BOSS 在线城市映射失败", "\n".join(logs.output))

    def test_fetch_boss_json_rejects_nonzero_business_code(self):
        """HTTP 200 下的 code: 35 不能静默当作空城市表。"""
        module = load_module()
        response = mock.MagicMock()
        response.read.return_value = json.dumps({
            "code": 35,
            "message": "您的IP地址存在异常行为.",
            "zpData": {},
        }).encode("utf-8")
        response.__enter__.return_value = response

        with mock.patch.object(module, "urlopen", return_value=response):
            with self.assertRaisesRegex(module.CityAPIResponseError,
                                        "code=35"):
                module.fetch_boss_json(module.HOT_CITY_URL)

    def test_main_rejects_unknown_city_before_search(self):
        """CLI 城市预校验失败后以非零状态退出，不进入目标搜索。"""
        module = load_module()

        with mock.patch.object(sys, "argv", [
                "boss_cdp_raw.py", "--city", "不存在市",
        ]), \
             mock.patch.object(module, "require_runtime_dependencies",
                               return_value=True), \
             mock.patch.object(module, "resolve_city",
                               side_effect=module.CityResolutionError("无法解析城市")), \
             redirect_stdout(io.StringIO()) as output:
            with self.assertRaises(SystemExit) as exit_context:
                module.main()

        self.assertEqual(exit_context.exception.code, 1)
        self.assertIn("无法解析城市", output.getvalue())

    def test_resolve_city_empty_input(self):
        module = load_module()

        self.assertEqual(module.resolve_city(""), ("", ""))

    # ----- --list-cities -----

    def test_list_cities_prints_all(self):
        """--list-cities 打印全部城市（用本地码表，mock 掉联网）。"""
        module = load_module()

        with mock.patch.object(module, "load_live_city_maps",
                               return_value=({}, {})):
            with mock.patch("sys.stdout", new_callable=__import__("io").StringIO) as out:
                module.list_cities(keyword=None)
            text = out.getvalue()
        self.assertIn("个城市", text)
        self.assertIn("上海", text)
        self.assertIn("赣州", text)

    def test_list_cities_with_filter(self):
        """关键词过滤只打印匹配的城市。"""
        module = load_module()

        with mock.patch.object(module, "load_live_city_maps",
                               return_value=({}, {})):
            with mock.patch("sys.stdout", new_callable=__import__("io").StringIO) as out:
                module.list_cities(keyword="江")
            text = out.getvalue()
        self.assertIn("江", text)
        self.assertNotIn("上海", text)
        self.assertNotIn("赣州", text)

    def test_list_cities_offline_uses_local(self):
        """联网失败时回退本地静态码表，不报错。"""
        module = load_module()

        with mock.patch.object(module, "load_live_city_maps",
                               return_value=({}, {})):
            with mock.patch("sys.stdout", new_callable=__import__("io").StringIO) as out:
                module.list_cities(keyword=None)
            text = out.getvalue()
        # 本地码表非空时应有输出
        self.assertIn("个城市", text)

    def test_filter_maps_match_current_boss_condition_snapshot(self):
        module = load_module()

        self.assertEqual(
            module.SALARY_MAP,
            {
                "不限": "0",
                "3K以下": "402",
                "3-5K": "403",
                "5-10K": "404",
                "10-20K": "405",
                "20-50K": "406",
                "50K以上": "407",
            },
        )
        self.assertEqual(
            module.EXPERIENCE_MAP,
            {
                "不限": "0",
                "在校生": "108",
                "应届生": "102",
                "经验不限": "101",
                "1年以内": "103",
                "1-3年": "104",
                "3-5年": "105",
                "5-10年": "106",
                "10年以上": "107",
            },
        )
        self.assertEqual(
            module.DEGREE_MAP,
            {
                "不限": "0",
                "初中及以下": "209",
                "中专/中技": "208",
                "高中": "206",
                "大专": "202",
                "本科": "203",
                "硕士": "204",
                "博士": "205",
            },
        )

    def test_run_check_is_local_only_and_does_not_probe_boss(self):
        module = load_module()
        response = mock.Mock()
        response.json.return_value = {"Browser": "Chrome/140"}
        requests_mock = mock.Mock()
        requests_mock.get.return_value = response
        stdout = io.StringIO()
        with mock.patch.object(module, "require_runtime_dependencies", return_value=True), \
                mock.patch.object(module, "requests", requests_mock), \
                redirect_stdout(stdout):
            self.assertEqual(module.run_check(cdp_port=9333), 0)

        output = stdout.getvalue()
        self.assertIn("未主动探测", output)

    def test_detail_record_preserves_job_id_and_job_link(self):
        module = load_module()
        job = {
            "job_id": "abc123",
            "title": "AI Engineer",
            "boss_name": "Acme",
            "salary": "30-60K",
            "salary_source": "api",
            "location": "上海",
            "tags": "3-5年 | 本科",
            "job_link": "https://www.zhipin.com/job_detail/abc.html",
        }
        extracted = {
            "tags": ["Python"],
            "jd": "Build AI agents",
            "boss_active_status": "今日活跃",
        }

        detail = module.build_detail_record(job, extracted)

        self.assertEqual(detail["job_id"], "abc123")
        self.assertEqual(detail["job_link"], job["job_link"])
        self.assertEqual(detail["link"], job["job_link"])
        self.assertEqual(detail["salary"], "30-60K")
        self.assertEqual(detail["salary_source"], "api")
        self.assertEqual(detail["boss_active_status"], "今日活跃")

    def test_detail_record_preserves_explicit_job_status(self):
        module = load_module()
        detail = module.build_detail_record(
            {"job_id": "abc123", "job_link": "https://www.zhipin.com/job_detail/abc.html"},
            {"job_status": "招聘中", "contact_available": True, "jd": "Build AI agents", "tags": []},
        )

        self.assertEqual(detail["job_status"], "招聘中")
        self.assertTrue(detail["contact_available"])

    def test_detail_record_marks_unknown_job_status_without_closing_it(self):
        module = load_module()
        detail = module.build_detail_record(
            {"job_id": "abc123", "job_link": "https://www.zhipin.com/job_detail/abc.html"},
            {"job_status": "仍可浏览", "jd": "Build AI agents", "tags": []},
        )

        self.assertEqual(detail["job_status"], "未显式标注")

    def test_detail_record_falls_back_to_list_active_status(self):
        module = load_module()
        job = {
            "job_id": "abc123",
            "title": "AI Engineer",
            "boss_name": "Acme",
            "salary": "30-60K",
            "salary_source": "api",
            "location": "上海",
            "tags": "3-5年 | 本科",
            "job_link": "https://www.zhipin.com/job_detail/abc.html",
            "boss_active_status": "本周活跃",
        }
        extracted = {"tags": ["Python"], "jd": "Build AI agents"}

        detail = module.build_detail_record(job, extracted)

        self.assertEqual(detail["boss_active_status"], "本周活跃")

    def test_detail_extractor_never_uses_body_text_as_jd_fallback(self):
        module = load_module()

        self.assertNotIn("jd = body.substring", module.EXTRACT_DETAIL_JS)
        self.assertIn("page_text", module.EXTRACT_DETAIL_JS)
        self.assertIn("text.indexOf('职位描述')", module.EXTRACT_DETAIL_JS)

    def test_extract_job_description_removes_header_and_recruiter_footer(self):
        module = load_module()
        description = (
            "公司介绍\n这段属于招聘方发布的岗位正文，应当保留。\n"
            + "负责 AI 产品规划、需求分析、研发协作和上线复盘。\n" * 8
        ).strip()
        page_text = (
            "微信扫码分享 举报\n职位描述\n"
            f"{description}\n"
            "张女士\n今日活跃\n示例公司\n·\n招聘者\n竞争力分析\n"
            "查看完整个人竞争力\nBOSS 安全提示\n公司工商信息\n更多职位"
        )

        jd = module.extract_job_description({"jd": page_text, "page_text": page_text})

        self.assertEqual(jd, description)
        self.assertIn("公司介绍", jd)
        self.assertNotIn("张女士", jd)
        self.assertNotIn("竞争力分析", jd)

    def test_extract_job_description_rejects_login_truncation(self):
        module = load_module()
        page_text = (
            "职位描述\n负责产品规划和需求分析。\n"
            "登录查看完整内容\n招聘者\nBOSS 安全提示"
        )

        with self.assertRaises(module.DetailLoginRequiredError):
            module.extract_job_description({"jd": "", "page_text": page_text})

    def test_extract_job_description_preserves_competitiveness_heading_in_jd(self):
        module = load_module()
        description = (
            "岗位职责\n负责产品规划、需求分析和跨团队项目推进。\n"
            "竞争力分析\n负责持续研究竞品并制定差异化产品策略。\n" * 5
        )

        jd = module.extract_job_description({"jd": f"职位描述\n{description}"})

        self.assertIn("竞争力分析", jd)
        self.assertIn("制定差异化产品策略", jd)

    def test_extract_job_description_removes_trailing_recruiter_card(self):
        module = load_module()
        description = "负责 AI 产品规划、需求分析和跨团队项目推进。\n" * 8
        page_text = (
            f"职位描述\n{description}"
            "李女士\n在线\n示例公司\n·\n招聘专员"
        )

        jd = module.extract_job_description({"jd": page_text, "page_text": page_text})

        self.assertEqual(jd, description.strip())
        self.assertNotIn("李女士", jd)
        self.assertNotIn("招聘专员", jd)

    def test_extract_detail_fields_returns_boss_active_status_separately(self):
        module = load_module()
        description = (
            "公司介绍\n这段属于招聘方发布的岗位正文，应当保留。\n"
            + "负责 AI 产品规划、需求分析、研发协作和上线复盘。\n" * 8
        ).strip()
        page_text = (
            "微信扫码分享 举报\n职位描述\n"
            f"{description}\n"
            "张女士\n今日活跃\n示例公司\n·\n招聘者\n竞争力分析\n"
            "查看完整个人竞争力\nBOSS 安全提示\n公司工商信息\n更多职位"
        )

        fields = module.extract_detail_fields({"jd": page_text, "page_text": page_text})

        self.assertEqual(fields["jd"], description)
        self.assertEqual(fields["boss_active_status"], "今日活跃")
        self.assertEqual(fields["job_status"], "未显式标注")
        self.assertNotIn("今日活跃", fields["jd"])
        self.assertNotIn("张女士", fields["jd"])

    def test_extract_detail_fields_preserves_explicit_open_and_closed_status(self):
        module = load_module()
        description = "负责 Agent 应用开发和离线评测。\n" * 8

        open_fields = module.extract_detail_fields({
            "jd": "职位描述\n" + description,
            "job_status": "招聘中",
        })
        closed_fields = module.extract_detail_fields({
            "jd": "职位描述\n" + description,
            "job_status": "已关闭",
        })

        self.assertEqual(open_fields["job_status"], "招聘中")
        self.assertEqual(closed_fields["job_status"], "已关闭")

    def test_detail_status_extractor_covers_visible_closed_label(self):
        module = load_module()

        self.assertIn("职位已关闭", module.EXTRACT_DETAIL_JS)
        self.assertIn("停止招聘", module.EXTRACT_DETAIL_JS)

    def test_extract_detail_fields_online_status(self):
        module = load_module()
        description = "负责 AI 产品规划、需求分析和跨团队项目推进。\n" * 8
        page_text = (
            f"职位描述\n{description}"
            "李女士\n在线\n示例公司\n·\n招聘专员"
        )

        fields = module.extract_detail_fields({"jd": page_text, "page_text": page_text})

        self.assertEqual(fields["jd"], description.strip())
        self.assertEqual(fields["boss_active_status"], "在线")
        self.assertNotIn("在线", fields["jd"])

    def test_map_list_boss_active_status_from_representative_responses(self):
        module = load_module()

        # List API typically has bossOnline but not activeTimeDesc.
        self.assertEqual(
            module.map_list_boss_active_status({"bossOnline": True}),
            "在线",
        )
        # Prefer detailed label when list unexpectedly has activeTimeDesc.
        self.assertEqual(
            module.map_list_boss_active_status({
                "activeTimeDesc": "刚刚活跃",
                "bossOnline": True,
            }),
            "刚刚活跃",
        )
        self.assertEqual(module.map_list_boss_active_status({}), "")
        self.assertEqual(
            module.map_list_boss_active_status({"bossOnline": False}),
            "",
        )

    def test_resolve_boss_active_status_prefers_detail_over_list(self):
        module = load_module()

        self.assertEqual(
            module.resolve_boss_active_status(
                list_status="在线",
                detail_status="刚刚活跃",
            ),
            "刚刚活跃",
        )
        self.assertEqual(
            module.resolve_boss_active_status(list_status="在线", detail_status=""),
            "在线",
        )
        self.assertEqual(
            module.resolve_boss_active_status(list_status="", detail_status=""),
            "",
        )

    def test_extract_job_description_removes_recruiter_card_before_safety_footer(self):
        module = load_module()
        description = "负责视觉算法研发、模型部署和业务场景落地。\n" * 8
        page_text = (
            f"职位描述\n{description}"
            "认证资质\n人力资源服务许可证\n"
            "曾先生\n示例猎头\n·\n猎头顾问\n\n"
            "BOSS 安全提示\n公司介绍\n更多职位"
        )

        jd = module.extract_job_description({"jd": page_text, "page_text": page_text})

        self.assertEqual(
            jd,
            f"{description}认证资质\n人力资源服务许可证".strip(),
        )
        self.assertNotIn("曾先生", jd)
        self.assertNotIn("猎头顾问", jd)

    def test_extract_job_description_rejects_navigation_page(self):
        module = load_module()
        page_text = "首页\n职位\n公司\n校园\n无障碍专区\n热门职位\n产品经理"

        with self.assertRaisesRegex(module.DetailExtractionError, "navigation chrome"):
            module.extract_job_description({"jd": "", "page_text": page_text})

    def test_extract_job_description_rejects_short_text(self):
        module = load_module()

        with self.assertRaisesRegex(module.DetailExtractionError, "too short"):
            module.extract_job_description({"jd": "职位描述\n只有一句话"})

    def test_detail_url_adds_security_context_without_changing_job_link(self):
        module = load_module()
        job = {
            "job_link": "https://www.zhipin.com/job_detail/abc.html",
            "security_id": "sec value",
            "lid": "lid-123",
        }

        detail_url = module.build_detail_url(job)

        self.assertEqual(job["job_link"], "https://www.zhipin.com/job_detail/abc.html")
        self.assertEqual(
            detail_url,
            "https://www.zhipin.com/job_detail/abc.html?lid=lid-123&securityId=sec+value",
        )

    def test_dom_fallback_is_opt_in(self):
        module = load_module()

        self.assertFalse(module.should_use_dom_fallback([], allow_dom_fallback=False))
        self.assertTrue(module.should_use_dom_fallback([], allow_dom_fallback=True))
        self.assertFalse(module.should_use_dom_fallback([{"title": "Java"}], allow_dom_fallback=True))

    def test_api_job_parser_rejects_error_rows(self):
        module = load_module()

        self.assertEqual(module.parse_api_jobs_eval_value(json.dumps([{"error": 403}])), [])
        self.assertEqual(
            module.parse_api_jobs_eval_value(json.dumps([{"title": "Java", "job_link": "https://example.com"}])),
            [{"title": "Java", "job_link": "https://example.com"}],
        )

    def test_windows_default_paths_use_localappdata(self):
        module = load_module()
        env = {
            "LOCALAPPDATA": r"C:\Users\leon\AppData\Local",
            "PROGRAMFILES": r"C:\Program Files",
            "PROGRAMFILES(X86)": r"C:\Program Files (x86)",
        }
        expected_chrome = r"C:\Users\leon\AppData\Local\Google\Chrome\Application\chrome.exe"
        with mock.patch.object(module.platform, "system", return_value="Windows"), \
                mock.patch.dict(module.os.environ, env, clear=False), \
                mock.patch.object(module.os.path, "exists", side_effect=lambda p: p == expected_chrome):
            self.assertEqual(module.get_default_chrome_path(), expected_chrome)
            self.assertEqual(
                module.get_default_profile_dir(),
                r"C:\Users\leon\AppData\Local\Google\Chrome\User Data",
            )

    def test_windows_process_parsing_matches_user_data_dir_and_cdp_port(self):
        module = load_module()
        ps_json = json.dumps([{
            "ProcessId": 456,
            "CommandLine": (
                r'"C:\Program Files\Google\Chrome\Application\chrome.exe" '
                r'--remote-debugging-port=9333 '
                r'--user-data-dir="C:\Users\leon\.boss-zhipin-scraper\chrome-profile"'
            ),
        }])
        with mock.patch.object(module.platform, "system", return_value="Windows"), \
                mock.patch.object(module.subprocess, "run", return_value=type("Completed", (), {"stdout": ps_json, "returncode": 0})()):
            self.assertEqual(
                module.chrome_pids_for_user_data_dir(r"C:\Users\leon\.boss-zhipin-scraper\chrome-profile"),
                [456],
            )
            self.assertEqual(
                module.chrome_user_data_dirs_for_cdp_port(9333),
                [r"C:\Users\leon\.boss-zhipin-scraper\chrome-profile"],
            )

    def test_smoke_jobs_require_api_salary_and_link(self):
        module = load_module()

        self.assertTrue(module.has_usable_smoke_jobs([{
            "title": "AI Engineer",
            "salary": "30-60K",
            "salary_source": "api",
            "job_link": "https://www.zhipin.com/job_detail/abc.html",
        }]))
        self.assertFalse(module.has_usable_smoke_jobs([{
            "title": "AI Engineer",
            "salary": "",
            "salary_source": "api_empty",
            "job_link": "https://www.zhipin.com/job_detail/abc.html",
        }]))

    def test_write_detail_csv_exports_detail_fields(self):
        module = load_module()
        with tempfile_profile() as paths:
            csv_path = paths["cdp_profile"] / "details.csv"
            module.write_detail_csv(str(csv_path), [{
                "job_id": "abc123",
                "title": "AI Engineer",
                "company": "Acme",
                "salary": "30-60K",
                "salary_source": "api",
                "location": "上海",
                "tags_list": "3-5年 | 本科",
                "job_link": "https://www.zhipin.com/job_detail/abc.html",
                "skill_tags": ["Python", "LLM"],
                "jd": "Build AI agents",
            }])

            with open(csv_path, encoding="utf-8-sig", newline="") as f:
                rows = list(csv.DictReader(f))

        self.assertEqual(rows[0]["job_id"], "abc123")
        self.assertEqual(rows[0]["salary_source"], "api")
        self.assertEqual(rows[0]["skill_tags"], "Python | LLM")
        self.assertEqual(rows[0]["jd"], "Build AI agents")

    def test_scrape_details_final_save_handles_bare_filename(self):
        """--detail-output 传不带目录的裸文件名时，最终保存不应崩溃。

        空 jobs 列表不触发 CDP，可直接走到最终保存逻辑；此前最终保存用
        os.makedirs(os.path.dirname(path))，dirname 为空字符串会抛
        FileNotFoundError，丢掉收尾保存和 CSV 导出。
        """
        module = load_module()
        with tempfile_profile() as paths:
            workdir = paths["cdp_profile"]
            workdir.mkdir(parents=True, exist_ok=True)
            cwd = os.getcwd()
            os.chdir(workdir)
            try:
                module.scrape_details({"jobs": []}, output_path="boss_details.json")
                self.assertTrue((workdir / "boss_details.json").exists())
            finally:
                os.chdir(cwd)

    def test_scrape_details_stops_before_writing_login_truncation(self):
        module = load_module()
        session = mock.Mock()

        def send(method, params=None, sid=None):
            if method == "Target.createTarget":
                return {"result": {"targetId": "target-1"}}
            if method == "Target.attachToTarget":
                return {"result": {"sessionId": "session-1"}}
            return {}

        session.send.side_effect = send
        session.eval_js.side_effect = lambda script, sid: (
            json.dumps(
                {
                    "jd": "",
                    "page_text": "职位描述\n负责产品规划\n登录查看完整内容",
                    "tags": [],
                }
            )
            if script == module.EXTRACT_DETAIL_JS
            else None
        )
        job = {
            "job_id": "blocked",
            "title": "AI Product Manager",
            "job_link": "https://www.zhipin.com/job_detail/blocked.html",
        }

        with tempfile_profile() as paths:
            output = paths["cdp_profile"] / "details.json"
            with mock.patch.object(module, "CDPSession", return_value=session), \
                    mock.patch.object(module.time, "sleep"):
                with self.assertRaisesRegex(RuntimeError, "login expired"):
                    module.scrape_details({"jobs": [job]}, output_path=str(output))

        self.assertFalse(output.exists())
        session.send.assert_any_call(
            "Target.closeTarget", {"targetId": "target-1"}
        )
        session.close.assert_called_once()

    def test_setup_defaults_do_not_copy_cookies_or_kill_all_chrome(self):
        module = load_module()
        calls = {"copy2": [], "run": [], "popen": []}
        fake_requests = mock.Mock()
        responses = iter([
            Exception("not ready"),
            type("Resp", (), {"status_code": 200})(),
        ])

        def fake_get(*args, **kwargs):
            response = next(responses)
            if isinstance(response, Exception):
                raise response
            return response

        with tempfile_profile() as paths:
            expected_profile_arg = f"--user-data-dir={paths['cdp_profile']}"
            with mock.patch.object(module, "DEFAULT_PROFILE_DIR", str(paths["source_profile"])), \
                    mock.patch.object(module, "DEFAULT_CDP_DATA_DIR", str(paths["cdp_profile"])), \
                    mock.patch.object(module, "requests", fake_requests), \
                    mock.patch.object(module.shutil, "copy2", side_effect=lambda src, dst: calls["copy2"].append((src, dst))), \
                    mock.patch.object(module.subprocess, "run", side_effect=lambda *args, **kwargs: fake_run(calls, *args, **kwargs)), \
                    mock.patch.object(module.subprocess, "Popen", side_effect=lambda cmd, **kwargs: calls["popen"].append(cmd)), \
                    mock.patch.object(module.time, "sleep", return_value=None):
                fake_requests.get.side_effect = fake_get
                self.assertEqual(module.run_setup_chrome(cdp_port=9333), 0)

        self.assertEqual(calls["copy2"], [])
        self.assertTrue(all("killall" not in cmd for cmd in calls["run"]))
        self.assertTrue(calls["popen"])
        launched = calls["popen"][0]
        self.assertIn(expected_profile_arg, launched)

    def test_console_encoding_uses_utf8_with_replacement_when_supported(self):
        module = load_module()

        class FakeStream:
            def __init__(self):
                self.calls = []

            def reconfigure(self, **kwargs):
                self.calls.append(kwargs)

        stdout = FakeStream()
        stderr = FakeStream()
        with mock.patch.object(module.sys, "stdout", stdout), \
                mock.patch.object(module.sys, "stderr", stderr):
            module.configure_console_encoding()

        self.assertEqual(stdout.calls, [{"encoding": "utf-8", "errors": "replace"}])
        self.assertEqual(stderr.calls, [{"encoding": "utf-8", "errors": "replace"}])

    def test_copy_login_state_is_explicit_and_does_not_copy_password_databases(self):
        module = load_module()
        copied = []
        with tempfile_profile() as paths:
            with mock.patch.object(module, "DEFAULT_PROFILE_DIR", str(paths["source_profile"])), \
                    mock.patch.object(module, "DEFAULT_CDP_DATA_DIR", str(paths["cdp_profile"])), \
                    mock.patch.object(module.shutil, "copy2", side_effect=lambda src, dst: copied.append((pathlib.Path(src), pathlib.Path(dst)))):
                result = module.prepare_cdp_profile(copy_login_state=True, reset=False)

        copied_names = [src.name for src, _ in copied]
        copied_rel_paths = [src.relative_to(paths["source_profile"]) for src, _ in copied]
        self.assertEqual(result["copied"], 4)
        self.assertIn("Local State", copied_names)
        self.assertIn("Cookies", copied_names)
        self.assertIn(pathlib.Path("Default/Cookies-journal"), copied_rel_paths)
        self.assertIn(pathlib.Path("Default/Network/Cookies"), copied_rel_paths)
        self.assertNotIn("Login Data", copied_names)
        self.assertNotIn("Web Data", copied_names)

    def test_setup_rejects_ready_cdp_port_owned_by_other_profile(self):
        module = load_module()
        fake_requests = mock.Mock()
        fake_requests.get.return_value = type("Resp", (), {"status_code": 200})()

        with tempfile_profile() as paths:
            ps_output = (
                "123 /Applications/Google Chrome.app/Contents/MacOS/Google Chrome "
                "--remote-debugging-port=9333 --user-data-dir=/tmp/chrome-cdp-data\n"
            )
            with mock.patch.object(module, "DEFAULT_CDP_DATA_DIR", str(paths["cdp_profile"])), \
                    mock.patch.object(module, "requests", fake_requests), \
                    mock.patch.object(module.subprocess, "run", return_value=type("Completed", (), {"stdout": ps_output, "returncode": 0})()), \
                    mock.patch.object(module.subprocess, "Popen") as popen:
                self.assertEqual(module.run_setup_chrome(cdp_port=9333), 1)

        popen.assert_not_called()

    def test_setup_reuses_ready_cdp_port_owned_by_dedicated_profile(self):
        module = load_module()
        fake_requests = mock.Mock()
        fake_requests.get.return_value = type("Resp", (), {"status_code": 200})()

        with tempfile_profile() as paths:
            ps_output = (
                "123 /Applications/Google Chrome.app/Contents/MacOS/Google Chrome "
                f"--remote-debugging-port=9333 --user-data-dir={paths['cdp_profile']}\n"
            )
            with mock.patch.object(module, "DEFAULT_CDP_DATA_DIR", str(paths["cdp_profile"])), \
                    mock.patch.object(module, "requests", fake_requests), \
                    mock.patch.object(module.subprocess, "run", return_value=type("Completed", (), {"stdout": ps_output, "returncode": 0})()), \
                    mock.patch.object(module.subprocess, "Popen") as popen:
                self.assertEqual(module.run_setup_chrome(cdp_port=9333), 0)

        popen.assert_not_called()

    def test_setup_does_not_probe_login_automatically(self):
        module = load_module()
        fake_requests = mock.Mock()
        fake_requests.get.return_value = type("Resp", (), {"status_code": 200})()

        with tempfile_profile() as paths:
            ps_output = (
                "123 /Applications/Google Chrome.app/Contents/MacOS/Google Chrome "
                f"--remote-debugging-port=9333 --user-data-dir={paths['cdp_profile']}\n"
            )
            with mock.patch.object(module, "DEFAULT_CDP_DATA_DIR", str(paths["cdp_profile"])), \
                    mock.patch.object(module, "requests", fake_requests), \
                    mock.patch.object(module.subprocess, "run", return_value=type("Completed", (), {"stdout": ps_output, "returncode": 0})()):
                self.assertEqual(module.run_setup_chrome(cdp_port=9333), 0)

    def test_chrome_process_parsing_matches_unquoted_user_data_dir(self):
        module = load_module()

        with tempfile_profile() as paths:
            ps_output = (
                "123 /Applications/Google Chrome.app/Contents/MacOS/Google Chrome "
                f"--remote-debugging-port=9333 --user-data-dir={paths['cdp_profile']}\n"
                "456 /Applications/Google Chrome.app/Contents/MacOS/Google Chrome "
                "--remote-debugging-port=9334 --user-data-dir=/tmp/other-profile\n"
            )
            with mock.patch.object(module.subprocess, "run", return_value=type("Completed", (), {"stdout": ps_output, "returncode": 0})()):
                self.assertEqual(module.chrome_pids_for_user_data_dir(str(paths["cdp_profile"])), [123])
                self.assertEqual(module.chrome_user_data_dirs_for_cdp_port(9333), [str(paths["cdp_profile"])])
                self.assertTrue(module.cdp_port_uses_profile(9333, str(paths["cdp_profile"])))

    def test_stop_cdp_chrome_terminates_only_matching_profile(self):
        module = load_module()

        terminated = []
        # chrome_pids_for_user_data_dir 第一次返回 scraper profile 的 pid（111），
        # SIGTERM 后轮询返回空 -> 成功关闭，不升级 SIGKILL。
        # （按 user-data-dir 过滤出 111、不关其它 profile 的进程，该过滤逻辑由
        #   test_chrome_process_parsing_matches_unquoted_user_data_dir 独立覆盖）
        pid_lookups = iter([[111], []])
        with mock.patch.object(module, "chrome_pids_for_user_data_dir",
                               side_effect=lambda _dir: next(pid_lookups)), \
             mock.patch.object(module, "terminate_process",
                               side_effect=lambda pid, force=False: terminated.append((pid, force))), \
             mock.patch.object(module.time, "sleep"):
            stopped = module.stop_cdp_chrome("/fake/scraper-profile")

        self.assertEqual(stopped, 1)
        # 只对 scraper 的 pid 用 SIGTERM（force=False），且只一次
        self.assertEqual(terminated, [(111, False)])

    def test_stop_cdp_chrome_no_processes_returns_zero(self):
        module = load_module()

        with mock.patch.object(module, "chrome_pids_for_user_data_dir", return_value=[]):
            stopped = module.stop_cdp_chrome("/fake/scraper-profile")
        self.assertEqual(stopped, 0)

    def test_stop_cdp_chrome_escalates_to_force_kill(self):
        module = load_module()

        terminated = []
        # SIGTERM 后进程始终在 -> 轮询 10 次都不为空 -> 升级 SIGKILL
        with mock.patch.object(module, "chrome_pids_for_user_data_dir", return_value=[333]), \
             mock.patch.object(module, "terminate_process",
                               side_effect=lambda pid, force=False: terminated.append((pid, force))), \
             mock.patch.object(module.time, "sleep"):
            stopped = module.stop_cdp_chrome("/fake/scraper-profile")

        self.assertEqual(stopped, 1)
        # 先 SIGTERM（force=False），10 次轮询后升级 SIGKILL（force=True）
        self.assertIn((333, False), terminated)
        self.assertIn((333, True), terminated)
        self.assertLess(terminated.index((333, False)), terminated.index((333, True)))

    def test_run_stop_chrome_closes_dedicated_profile(self):
        module = load_module()

        with tempfile_profile() as paths:
            scraper_dir = str(paths["cdp_profile"])
            captured = {}

            def fake_prepare(**kwargs):
                # run_stop_chrome 必须以 copy_login_state=False, reset=False 调用（只定位，不动 profile）
                captured["prepare_kwargs"] = kwargs
                return {"path": scraper_dir, "copied": 0, "reset": False, "copy_login_state": False}

            def fake_stop(directory):
                captured["stopped_dir"] = directory
                return 1

            with mock.patch.object(module, "require_runtime_dependencies", return_value=True), \
                 mock.patch.object(module, "prepare_cdp_profile", side_effect=fake_prepare), \
                 mock.patch.object(module, "stop_cdp_chrome", side_effect=fake_stop):
                rc = module.run_stop_chrome()

            self.assertEqual(rc, 0)
            # 只定位 profile，绝不复制登录态 / 重置
            self.assertEqual(captured["prepare_kwargs"], {"copy_login_state": False, "reset": False})
            # 关的就是 scraper 隔离 profile 目录
            self.assertEqual(captured["stopped_dir"], scraper_dir)

    def test_run_stop_chrome_returns_zero_when_no_chrome_running(self):
        module = load_module()

        with tempfile_profile() as paths:
            scraper_dir = str(paths["cdp_profile"])
            with mock.patch.object(module, "require_runtime_dependencies", return_value=True), \
                 mock.patch.object(module, "prepare_cdp_profile",
                                   return_value={"path": scraper_dir, "copied": 0,
                                                 "reset": False, "copy_login_state": False}), \
                 mock.patch.object(module, "stop_cdp_chrome", return_value=0):
                rc = module.run_stop_chrome()
            self.assertEqual(rc, 0)

    def test_help_does_not_require_cdp_runtime_dependencies(self):
        result = subprocess.run(
            [sys.executable, str(SCRIPT_PATH), "--help"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
        )

        self.assertEqual(result.returncode, 0)
        self.assertIn("--setup-chrome", result.stdout)
        self.assertIn("--reset-chrome-profile", result.stdout)
        self.assertIn("--stop-chrome", result.stdout)
        self.assertIn("--close-chrome", result.stdout)


class tempfile_profile:
    def __enter__(self):
        import tempfile

        self.tmp = tempfile.TemporaryDirectory()
        root = pathlib.Path(self.tmp.name)
        source_profile = root / "Google" / "Chrome"
        default = source_profile / "Default"
        default.mkdir(parents=True)
        for name in ["Cookies", "Cookies-journal", "Login Data", "Web Data"]:
            (default / name).write_text(name, encoding="utf-8")
        network = default / "Network"
        network.mkdir()
        (network / "Cookies").write_text("network cookies", encoding="utf-8")
        (source_profile / "Local State").write_text("state", encoding="utf-8")
        self.paths = {
            "source_profile": source_profile,
            "cdp_profile": root / "persistent-profile",
        }
        return self.paths

    def __exit__(self, exc_type, exc, tb):
        self.tmp.cleanup()


def fake_run(calls, *args, **kwargs):
    calls["run"].append(args[0])
    return type("Completed", (), {"stdout": "", "returncode": 0})()


ROOT_PATH = SCRIPT_PATH.parents[1]


def _normalize_version(raw):
    """统一版本号格式，去掉 'v' 前缀和 patch 段，只比较 major.minor。

    README/SKILL.md 里常写成 'v2.0'，pyproject/脚本里是 '2.0.0'，
    只要 major.minor 一致即视为同步，避免 patch 号差异造成误报。
    """
    text = str(raw).strip().lstrip("vV")
    parts = text.split(".")
    major = parts[0] if len(parts) > 0 else "0"
    minor = parts[1] if len(parts) > 1 else "0"
    return f"{major}.{minor}"


class VersionConsistencyTests(unittest.TestCase):
    """校验版本号在 README / pyproject.toml / SKILL.md / 脚本四处保持一致。

    发版时只改一处会漏掉其他几处，这个测试在 CI/本地跑测试时就能拦住。
    """

    def _read_text(self, name):
        return (ROOT_PATH / name).read_text(encoding="utf-8")

    def test_script_version_is_defined(self):
        module = load_module()
        self.assertTrue(getattr(module, "__version__", None),
                        "脚本缺少 __version__")

    def test_versions_are_in_sync_across_all_sources(self):
        module = load_module()
        script_ver = _normalize_version(module.__version__)

        # pyproject.toml: version = "2.0.0"
        pyproject = self._read_text("pyproject.toml")
        m = re.search(r'^version\s*=\s*"([^"]+)"', pyproject, re.MULTILINE)
        self.assertIsNotNone(m, "pyproject.toml 未找到 version 字段")
        pyproject_ver = _normalize_version(m.group(1))

        # SKILL.md frontmatter: version: 2.0.0
        skill = self._read_text("SKILL.md")
        m = re.search(r"^version:\s*([^\n]+)$", skill, re.MULTILINE)
        self.assertIsNotNone(m, "SKILL.md 未找到 version 字段")
        skill_ver = _normalize_version(m.group(1))

        # README.md 标题: # ... v2.0
        readme = self._read_text("README.md")
        m = re.search(r"v(\d+\.\d+(?:\.\d+)?)", readme)
        self.assertIsNotNone(m, "README.md 未找到版本号")
        readme_ver = _normalize_version(m.group(1))

        self.assertEqual(script_ver, pyproject_ver,
                         f"脚本({script_ver}) 与 pyproject.toml({pyproject_ver}) 版本不一致")
        self.assertEqual(script_ver, skill_ver,
                         f"脚本({script_ver}) 与 SKILL.md({skill_ver}) 版本不一致")
        self.assertEqual(script_ver, readme_ver,
                         f"脚本({script_ver}) 与 README.md({readme_ver}) 版本不一致")


class NativeFlowRegressionTests(unittest.TestCase):
    def test_legacy_probe_and_injected_xhr_branches_are_removed(self):
        module = load_module()
        for name in (
            "LoginProbeStatus",
            "LoginProbeResult",
            "probe_login_state",
            "check_login_state",
            "FETCH_API_JS_TEMPLATE",
        ):
            self.assertFalse(hasattr(module, name), name)
        self.assertNotIn("XMLHttpRequest", module.EXTRACT_DETAIL_JS)

    def test_detail_field_normalization_removes_map_ui_suffix(self):
        module = load_module()
        fields = module.extract_detail_fields({
            "jd": "职位描述\n" + ("负责 Agent 应用开发。" * 20),
            "location": "北京海淀区丽金智地中心\n\n点击查看地图",
            "company_link": "https://www.zhipin.com/gongsi/company.html",
        }, min_length=10)
        self.assertEqual(fields["location"], "北京海淀区丽金智地中心")

    def test_direct_link_detail_record_fills_page_metadata(self):
        module = load_module()
        detail = module.build_detail_record(
            {"job_id": "abc", "job_link": "https://www.zhipin.com/job_detail/abc.html"},
            {
                "title": "Agent 实习生",
                "company": "示例公司",
                "salary": "300-400元/天",
                "location": "北京·海淀区",
                "company_link": "https://www.zhipin.com/gongsi/company.html",
                "tags": ["Python", "本科"],
                "jd": "负责 Agent 应用开发。" * 40,
                "boss_active_status": "今日活跃",
            },
        )
        self.assertEqual(detail["title"], "Agent 实习生")
        self.assertEqual(detail["company"], "示例公司")
        self.assertEqual(detail["salary"], "300-400元/天")
        self.assertEqual(detail["salary_source"], "detail_dom")
        self.assertEqual(detail["location"], "北京·海淀区")
        self.assertEqual(detail["tags_list"], "Python | 本科")
        self.assertEqual(detail["company_link"], "https://www.zhipin.com/gongsi/company.html")

    def test_detail_extractor_captures_visible_internship_constraints(self):
        module = load_module()
        self.assertIn(".job-limit span", module.EXTRACT_DETAIL_JS)
        self.assertIn("优秀论文优先", module.EXTRACT_DETAIL_JS)
        self.assertIn("工作(?:时长)?", module.EXTRACT_DETAIL_JS)

    def test_native_api_normalizer_rejects_business_error(self):
        module = load_module()
        with self.assertRaises(module.BossAPIError) as ctx:
            module.normalize_api_jobs({"code": 37, "message": "您的环境存在异常"})
        self.assertEqual(ctx.exception.code, 37)

    def test_homepage_payload_extracts_nested_selected_and_latest_jobs(self):
        module = load_module()
        payload = {
            "code": 0,
            "zpData": {
                "recommendJobList": [{
                    "jobName": "Agent 实习生",
                    "encryptJobId": "selected-id",
                    "salaryDesc": "300-400元/天",
                    "brandName": "示例公司",
                    "cityName": "成都",
                }],
                "latestJobList": [{
                    "jobInfo": {
                        "jobName": "大模型算法实习生",
                        "encryptJobId": "latest-id",
                        "salaryDesc": "400-500元/天",
                        "brandName": "新公司",
                        "cityName": "成都",
                    }
                }],
            },
        }

        jobs, sources = module.normalize_homepage_payload(
            payload, "https://www.zhipin.com/wapi/zpgeek/recommend/job/list.json"
        )

        self.assertEqual([job["title"] for job in jobs], [
            "Agent 实习生", "大模型算法实习生",
        ])
        self.assertEqual(jobs[0]["homepage_section"], "selected")
        self.assertEqual(jobs[1]["homepage_section"], "latest")
        self.assertEqual({source["section"] for source in sources}, {"selected", "latest"})

    def test_homepage_payload_rejects_business_error(self):
        module = load_module()
        with self.assertRaises(module.BossAPIError) as ctx:
            module.normalize_homepage_payload({"code": 37, "message": "您的环境存在异常"})
        self.assertEqual(ctx.exception.code, 37)

    def test_homepage_sort_type_maps_to_visible_sections(self):
        module = load_module()
        base = "https://www.zhipin.com/wapi/zpgeek/recommend/job/list.json"
        self.assertEqual(
            module.classify_homepage_section(base + "?sortType=1&page=1", "$.zpData.jobList"),
            "selected",
        )
        self.assertEqual(
            module.classify_homepage_section(base + "?sortType=2&page=1", "$.zpData.jobList"),
            "latest",
        )

    def test_homepage_capture_stops_after_both_native_sections_arrive(self):
        module = load_module()
        base = "https://www.zhipin.com/wapi/zpgeek/recommend/job/list.json"
        cdp = mock.Mock()
        cdp.recv_event.side_effect = [
            {"method": "Network.responseReceived", "params": {
                "requestId": "selected", "response": {
                    "url": base + "?sortType=1&page=1", "mimeType": "application/json",
                },
            }},
            {"method": "Network.loadingFinished", "params": {"requestId": "selected"}},
            {"method": "Network.responseReceived", "params": {
                "requestId": "latest", "response": {
                    "url": base + "?sortType=2&page=1", "mimeType": "application/json",
                },
            }},
            {"method": "Network.loadingFinished", "params": {"requestId": "latest"}},
        ]

        def response_body(_method, params, _sid, timeout=10):
            item = {
                "jobName": params["requestId"],
                "encryptJobId": params["requestId"] + "-id",
                "salaryDesc": "300-400元/天",
                "brandName": "示例公司",
                "cityName": "成都",
            }
            return {"result": {"body": json.dumps({
                "code": 0, "zpData": {"jobList": [item]},
            })}}

        cdp.send.side_effect = response_body
        jobs, sources = module.wait_for_homepage_job_responses(cdp, "sid", timeout=5)

        self.assertEqual([job["homepage_section"] for job in jobs], ["selected", "latest"])
        self.assertEqual(len(sources), 2)
        self.assertEqual(cdp.recv_event.call_count, 4)

    def test_inbox_schema_summary_keeps_only_keys_not_private_values(self):
        module = load_module()
        shapes = module.json_list_shapes({
            "conversationList": [{
                "encryptJobId": "private-job-id",
                "lastMessage": "private preview",
                "userName": "private person",
            }],
        })
        rendered = json.dumps(shapes, ensure_ascii=False)
        self.assertIn("conversationList", rendered)
        self.assertIn("lastMessage", rendered)
        self.assertNotIn("private preview", rendered)
        self.assertNotIn("private person", rendered)

    def test_websocket_schema_keeps_keys_but_removes_chat_text(self):
        module = load_module()
        schema = module.websocket_payload_schema(json.dumps({
            "type": "message",
            "data": {
                "content": "这是一条不应输出的私聊正文",
                "sender": "private recruiter",
                "messageId": "opaque-id",
            },
        }, ensure_ascii=False))
        rendered = json.dumps(schema, ensure_ascii=False)
        self.assertEqual(schema["encoding"], "json")
        self.assertIn("data", rendered)
        self.assertIn("content", rendered)
        self.assertNotIn("不应输出", rendered)
        self.assertNotIn("private recruiter", rendered)
        self.assertNotIn("opaque-id", rendered)

    def test_active_inbox_read_attaches_without_navigation_or_input(self):
        module = load_module()
        cdp = mock.Mock()

        def send(method, params=None, sid=None, timeout=30):
            if method == "Target.getTargets":
                return {"result": {"targetInfos": [{
                    "targetId": "chat-target",
                    "type": "page",
                    "url": "https://www.zhipin.com/web/geek/chat",
                }]}}
            if method == "Target.attachToTarget":
                return {"result": {"sessionId": "chat-session"}}
            return {"result": {}}

        cdp.send.side_effect = send
        cdp.eval_js.return_value = json.dumps({
            "expected_contact_in_active_header": True,
            "active_header": {"text": "刘姗｜HR", "top": 20, "left": 600},
            "rendered_entries": [
                {"type": "text_or_card", "text": "测试消息", "image_count": 0, "link_count": 0},
                {"type": "image_or_attachment", "text": "", "image_count": 1, "link_count": 0},
            ],
            "composer_controls": [{"tag": "textarea", "placeholder": ""}],
        }, ensure_ascii=False)

        with mock.patch.object(module, "CDPSession", return_value=cdp):
            result = module.read_active_inbox_conversation("刘姗", max_entries=10)

        self.assertEqual(result["rendered_message_count"], 2)
        self.assertEqual(result["message_type_counts"], {
            "text_or_card": 1, "image_or_attachment": 1,
        })
        calls = [call.args[0] for call in cdp.send.call_args_list]
        self.assertNotIn("Page.navigate", calls)
        self.assertFalse(any(method.startswith("Input.") for method in calls))

    def test_active_inbox_script_distinguishes_myself_and_system_rows(self):
        module = load_module()
        self.assertIn("item-(?:self|myself)", module.EXTRACT_ACTIVE_INBOX_JS)
        self.assertIn("item-system", module.EXTRACT_ACTIVE_INBOX_JS)

    def test_active_inbox_send_requires_confirmation_before_opening_cdp(self):
        module = load_module()
        with self.assertRaisesRegex(ValueError, "--confirm-send"):
            module.send_active_inbox_text("杨先生", "你好", confirmed=False)

    def test_active_inbox_send_targets_current_header_and_verifies_once(self):
        module = load_module()
        cdp = mock.Mock()

        def send(method, params=None, sid=None, timeout=30):
            if method == "Target.getTargets":
                return {"result": {"targetInfos": [{
                    "targetId": "chat-target", "type": "page",
                    "url": "https://www.zhipin.com/web/geek/chat",
                }]}}
            if method == "Target.attachToTarget":
                return {"result": {"sessionId": "chat-session"}}
            if method == "DOM.getDocument":
                return {"result": {"root": {"nodeId": 1}}}
            if method == "DOM.querySelector":
                return {"result": {"nodeId": 2}}
            return {"result": {}}

        cdp.send.side_effect = send
        cdp.eval_js.side_effect = [
            json.dumps({"matches": [{"text": "杨先生｜Loopit", "top": 20, "left": 600}]}),
            0,
            "你好",
            "你好",
            "",
            1,
        ]
        with mock.patch.object(module, "CDPSession", return_value=cdp), \
                mock.patch.object(module.time, "sleep"):
            result = module.send_active_inbox_text("杨先生", "你好", confirmed=True)

        self.assertTrue(result["submitted"])
        self.assertTrue(result["post_send_visible"])
        cdp.send.assert_any_call("DOM.focus", {"nodeId": 2}, "chat-session")
        cdp.send.assert_any_call("Input.insertText", {"text": "你好"}, "chat-session")
        key_calls = [call for call in cdp.send.call_args_list if call.args[0] == "Input.dispatchKeyEvent"]
        self.assertEqual(len(key_calls), 3)
        self.assertFalse(any(call.args[0] == "Page.navigate" for call in cdp.send.call_args_list))

    def test_inbox_discovery_url_must_stay_on_zhipin(self):
        module = load_module()
        with self.assertRaises(ValueError):
            module.discover_inbox_endpoints("https://example.com/", capture_seconds=5)

    def test_homepage_url_must_stay_on_zhipin(self):
        module = load_module()
        with self.assertRaises(ValueError):
            module.scrape_homepage("https://example.com/", "-", capture_seconds=5)

    def test_atomic_json_write_replaces_target(self):
        module = load_module()
        with tempfile_profile() as paths:
            target = paths["cdp_profile"] / "result.json"
            module.write_json_atomic(str(target), {"jobs": [{"job_id": "one"}]})
            module.write_json_atomic(str(target), {"jobs": [{"job_id": "two"}]})
            self.assertEqual(json.loads(target.read_text(encoding="utf-8"))["jobs"][0]["job_id"], "two")
            self.assertFalse(any(target.parent.glob(".boss-json-*.tmp")))


class ProjectScopeTests(unittest.TestCase):
    """项目边界守卫：只保留抓取和聚合分析，不内置简历匹配打分。"""

    def _read_text(self, name):
        return (ROOT_PATH / name).read_text(encoding="utf-8")

    def test_resume_matching_feature_is_not_packaged_or_documented(self):
        self.assertFalse(
            (ROOT_PATH / "scripts" / "resume_score.py").exists(),
            "简历匹配打分脚本不应作为项目功能保留",
        )
        self.assertFalse(
            (ROOT_PATH / "tests" / "test_resume_score.py").exists(),
            "删除简历匹配功能时也应删除对应测试",
        )

        combined = "\n".join(
            self._read_text(name)
            for name in ("README.md", "CHANGELOG.md", "SKILL.md", "pyproject.toml", "requirements.txt", "uv.lock")
        )
        for forbidden in (
            "resume_score",
            "pdfplumber",
            "pypdf",
            "python-docx",
            "openai",
            "langchain",
            "sentence-transformers",
            "简历匹配打分",
            "enable-llm",
        ):
            self.assertNotIn(forbidden, combined)


class SendModeTests(unittest.TestCase):
    VALID_LINK = "https://www.zhipin.com/job_detail/abc123.html?lid=1&securityId=2"
    SECOND_LINK = "https://www.zhipin.com/job_detail/def456.html?lid=3&securityId=4"

    def _contact_state(self, **overrides):
        state = {
            "status": "招聘中",
            "buttons": [{"label": "立即沟通", "visible": True}],
            "risk_markers": [],
            "url": self.VALID_LINK,
        }
        state.update(overrides)
        return json.dumps(state, ensure_ascii=False)

    def test_send_js_constants_cover_contact_and_composer(self):
        module = load_module()
        self.assertIn("立即沟通", module.CONTACT_BUTTON_STATE_JS)
        self.assertIn("继续沟通", module.CLICK_CONTACT_BUTTON_JS)
        self.assertIn("contenteditable", module.CHAT_COMPOSER_STATE_JS)
        self.assertIn("has_input", module.CHAT_COMPOSER_STATE_JS)
        self.assertIn("scrollIntoView", module.SCROLL_CONTACT_BUTTON_JS)

    def test_send_validates_content_and_links_before_cdp(self):
        module = load_module()
        with mock.patch.object(module, "CDPSession",
                               side_effect=AssertionError("不应连接 CDP")):
            with self.assertRaisesRegex(ValueError, "非空"):
                module.send_to_job_links(self.VALID_LINK, "  ")
            with self.assertRaisesRegex(ValueError, "--job_link"):
                module.send_to_job_links("", "你好")
            with self.assertRaisesRegex(ValueError, "非法 JD 链接"):
                module.send_to_job_links("https://example.com/x.html", "你好")
            with self.assertRaisesRegex(ValueError, "500"):
                module.send_to_job_links(self.VALID_LINK, "字" * 501)

    def test_send_main_rejects_missing_content_and_links(self):
        module = load_module()
        with mock.patch.object(sys, "argv", [
                "boss_cdp_raw.py", "--mode", "send", "--job_link", self.VALID_LINK,
        ]), \
             mock.patch.object(module, "require_runtime_dependencies",
                               return_value=True), \
             redirect_stdout(io.StringIO()):
            with self.assertRaises(SystemExit) as exit_context:
                module.main()
        self.assertEqual(exit_context.exception.code, 2)

        with mock.patch.object(sys, "argv", [
                "boss_cdp_raw.py", "--mode", "send", "--content", "你好",
        ]), \
             mock.patch.object(module, "require_runtime_dependencies",
                               return_value=True), \
             redirect_stdout(io.StringIO()):
            with self.assertRaises(SystemExit) as exit_context:
                module.main()
        self.assertEqual(exit_context.exception.code, 2)

    def test_send_main_stdout_emits_json_without_file(self):
        module = load_module()
        fake = {
            "mode": "send", "total": 1, "sent": 1,
            "skipped": 0, "aborted": 0, "results": [],
            "sent_at": "now", "scope": "",
        }
        with mock.patch.object(sys, "argv", [
                "boss_cdp_raw.py", "--mode", "send", "--content", "你好",
                "--job_link", self.VALID_LINK, "--stdout",
        ]), \
             mock.patch.object(module, "require_runtime_dependencies",
                               return_value=True), \
             mock.patch.object(module, "send_to_job_links",
                               return_value=fake), \
             mock.patch.object(module, "write_json_atomic") as write_mock, \
             redirect_stdout(io.StringIO()) as output:
            with self.assertRaises(SystemExit) as exit_context:
                module.main()
        self.assertEqual(exit_context.exception.code, 0)
        write_mock.assert_not_called()
        self.assertIn('"mode": "send"', output.getvalue())

    def test_send_main_writes_file_without_stdout(self):
        module = load_module()
        fake = {"mode": "send", "total": 1, "sent": 0, "skipped": 1,
                "aborted": 0, "results": [], "sent_at": "now", "scope": ""}
        with mock.patch.object(sys, "argv", [
                "boss_cdp_raw.py", "--mode", "send", "--content", "你好",
                "--job_link", self.VALID_LINK,
        ]), \
             mock.patch.object(module, "require_runtime_dependencies",
                               return_value=True), \
             mock.patch.object(module, "send_to_job_links",
                               return_value=fake), \
             mock.patch.object(module, "write_json_atomic") as write_mock, \
             redirect_stdout(io.StringIO()):
            with self.assertRaises(SystemExit) as exit_context:
                module.main()
        self.assertEqual(exit_context.exception.code, 0)
        write_mock.assert_called_once()

    def test_send_one_job_link_clicks_and_sends_once_in_place(self):
        module = load_module()
        cdp = mock.Mock()
        outgoing = {"count": 0}
        composer = {"text": ""}

        def eval_js(script, sid):
            if "composer_text" in script:
                return composer["text"]
            if "riskMarkers" in script and "buttons" in script:
                return self._contact_state()
            if "clicked" in script:
                return json.dumps({"clicked": "立即沟通"})
            if "has_input" in script:
                return json.dumps({"has_input": True, "tag": "contenteditable",
                                   "risk_markers": [], "url": "https://www.zhipin.com/web/geek/chat"})
            if "el.focus" in script:
                return json.dumps({"ok": True, "tag": "contenteditable"})
            if "message-item" in script:
                outgoing["count"] += 1
                return outgoing["count"]
            return None

        cdp.eval_js.side_effect = eval_js

        def send(method, params=None, sid=None, timeout=30):
            if method == "Input.insertText":
                composer["text"] += (params or {}).get("text", "")
                return {"result": {}}
            if method == "Input.dispatchKeyEvent":
                composer["text"] = ""
                return {"result": {}}
            if method == "Target.getTargets":
                return {"result": {"targetInfos": [
                    {"targetId": "jd-tid", "type": "page", "url": self.VALID_LINK},
                ]}}
            if method == "DOM.getDocument":
                return {"result": {"root": {"nodeId": 1}}}
            if method == "DOM.querySelector":
                return {"result": {"nodeId": 2}}
            return {"result": {}}

        cdp.send.side_effect = send
        with mock.patch.object(module, "create_page_session",
                               return_value=("jd-tid", "jd-sid")), \
             mock.patch.object(module.time, "sleep"):
            result = module.send_one_job_link(cdp, self.VALID_LINK, "你好")

        self.assertTrue(result["submitted"])
        self.assertTrue(result["post_send_visible"])
        self.assertEqual(result["message"], "你好")
        cdp.send.assert_any_call("Input.insertText", {"text": "你好"}, "jd-sid")
        key_calls = [call for call in cdp.send.call_args_list
                     if call.args[0] == "Input.dispatchKeyEvent"]
        self.assertEqual(len(key_calls), 3)
        cdp.send.assert_any_call("Target.closeTarget", {"targetId": "jd-tid"})
        self.assertFalse(any(call.args[0] == "Target.attachToTarget"
                             for call in cdp.send.call_args_list))

    def test_send_one_job_link_attaches_popup_chat_target(self):
        module = load_module()
        cdp = mock.Mock()
        outgoing = {"count": 0}
        composer = {"text": ""}
        get_targets_calls = {"n": 0}

        def eval_js(script, sid):
            if "composer_text" in script:
                return composer["text"]
            if "riskMarkers" in script and "buttons" in script:
                return self._contact_state(buttons=[
                    {"label": "继续沟通", "visible": True}])
            if "clicked" in script:
                return json.dumps({"clicked": "继续沟通"})
            if "has_input" in script:
                if sid == "jd-sid":
                    return json.dumps({"has_input": False, "tag": "",
                                       "risk_markers": [], "url": self.VALID_LINK})
                return json.dumps({"has_input": True, "tag": "contenteditable",
                                   "risk_markers": [], "url": "https://www.zhipin.com/web/geek/chat"})
            if "el.focus" in script:
                return json.dumps({"ok": True, "tag": "contenteditable"})
            if "message-item" in script:
                outgoing["count"] += 1
                return outgoing["count"]
            return None

        cdp.eval_js.side_effect = eval_js

        def send(method, params=None, sid=None, timeout=30):
            if method == "Input.insertText":
                composer["text"] += (params or {}).get("text", "")
                return {"result": {}}
            if method == "Input.dispatchKeyEvent":
                composer["text"] = ""
                return {"result": {}}
            if method == "Target.getTargets":
                get_targets_calls["n"] += 1
                targets = [{"targetId": "jd-tid", "type": "page", "url": self.VALID_LINK}]
                if get_targets_calls["n"] > 1:
                    targets.append({"targetId": "chat-tid", "type": "page",
                                    "url": "https://www.zhipin.com/web/geek/chat?lid=1&securityId=2"})
                return {"result": {"targetInfos": targets}}
            if method == "Target.attachToTarget":
                return {"result": {"sessionId": "chat-sid"}}
            if method == "DOM.getDocument":
                return {"result": {"root": {"nodeId": 1}}}
            if method == "DOM.querySelector":
                return {"result": {"nodeId": 2}}
            return {"result": {}}

        cdp.send.side_effect = send
        with mock.patch.object(module, "create_page_session",
                               return_value=("jd-tid", "jd-sid")), \
             mock.patch.object(module.time, "sleep"):
            result = module.send_one_job_link(cdp, self.VALID_LINK, "你好")

        self.assertTrue(result["submitted"])
        cdp.send.assert_any_call("Target.attachToTarget",
                                 {"targetId": "chat-tid", "flatten": True})
        cdp.send.assert_any_call("Input.insertText", {"text": "你好"}, "chat-sid")
        cdp.send.assert_any_call("Target.closeTarget", {"targetId": "chat-tid"})
        cdp.send.assert_any_call("Target.closeTarget", {"targetId": "jd-tid"})

    def test_send_one_job_link_falls_back_to_trusted_mouse_click(self):
        module = load_module()
        cdp = mock.Mock()
        outgoing = {"count": 0}
        composer = {"text": ""}
        mouse_sent = {"v": False}

        def eval_js(script, sid):
            if "composer_text" in script:
                return composer["text"]
            if "riskMarkers" in script and "buttons" in script:
                return self._contact_state()
            if "clicked" in script:
                return json.dumps({"clicked": "立即沟通"})
            if "scrollIntoView" in script:
                return True
            if "getBoundingClientRect" in script:
                return json.dumps({"x": 400, "y": 300, "visible": True})
            if "has_input" in script:
                if sid == "jd-sid":
                    return json.dumps({"has_input": False, "tag": "",
                                       "risk_markers": [], "url": self.VALID_LINK})
                return json.dumps({"has_input": True, "tag": "contenteditable",
                                   "risk_markers": [], "url": "https://www.zhipin.com/web/geek/chat"})
            if "el.focus" in script:
                return json.dumps({"ok": True, "tag": "contenteditable"})
            if "message-item" in script:
                outgoing["count"] += 1
                return outgoing["count"]
            return None

        cdp.eval_js.side_effect = eval_js

        def send(method, params=None, sid=None, timeout=30):
            if method == "Input.insertText":
                composer["text"] += (params or {}).get("text", "")
                return {"result": {}}
            if method == "Input.dispatchKeyEvent":
                composer["text"] = ""
                return {"result": {}}
            if method == "Input.dispatchMouseEvent":
                mouse_sent["v"] = True
                return {"result": {}}
            if method == "Target.getTargets":
                targets = [{"targetId": "jd-tid", "type": "page", "url": self.VALID_LINK}]
                if mouse_sent["v"]:
                    targets.append({"targetId": "chat-tid", "type": "page",
                                    "url": "https://www.zhipin.com/web/geek/chat"})
                return {"result": {"targetInfos": targets}}
            if method == "Target.attachToTarget":
                return {"result": {"sessionId": "chat-sid"}}
            if method == "DOM.getDocument":
                return {"result": {"root": {"nodeId": 1}}}
            if method == "DOM.querySelector":
                return {"result": {"nodeId": 2}}
            return {"result": {}}

        cdp.send.side_effect = send
        with mock.patch.object(module, "create_page_session",
                               return_value=("jd-tid", "jd-sid")), \
             mock.patch.object(module.time, "sleep"):
            result = module.send_one_job_link(cdp, self.VALID_LINK, "你好")

        self.assertTrue(result["submitted"])
        mouse_events = [call for call in cdp.send.call_args_list
                        if call.args[0] == "Input.dispatchMouseEvent"]
        self.assertEqual(len(mouse_events), 2)
        cdp.send.assert_any_call("Input.insertText", {"text": "你好"}, "chat-sid")

    def test_send_one_job_link_aborts_on_risk_control(self):
        module = load_module()
        cdp = mock.Mock()
        cdp.eval_js.side_effect = lambda script, sid: self._contact_state(
            status="", buttons=[], risk_markers=["环境异常"])
        with mock.patch.object(module, "create_page_session",
                               return_value=("jd-tid", "jd-sid")):
            with self.assertRaisesRegex(module.SendRiskControlError, "环境异常"):
                module.send_one_job_link(cdp, self.VALID_LINK, "你好")

    def test_send_one_job_link_skips_closed_job(self):
        module = load_module()
        cdp = mock.Mock()
        cdp.eval_js.side_effect = lambda script, sid: self._contact_state(
            status="已关闭")
        with mock.patch.object(module, "create_page_session",
                               return_value=("jd-tid", "jd-sid")):
            with self.assertRaisesRegex(module.SendJobUnavailableError, "已关闭"):
                module.send_one_job_link(cdp, self.VALID_LINK, "你好")

    def test_send_batch_skips_unavailable_and_counts(self):
        module = load_module()
        cdp = mock.Mock()

        def fake_send_one(cdp_arg, link, content):
            if link == self.VALID_LINK:
                raise module.SendJobUnavailableError("职位已关闭，跳过")
            return {"submitted": True, "post_send_visible": True,
                    "message": content}

        with mock.patch.object(module, "CDPSession", return_value=cdp), \
             mock.patch.object(module.time, "sleep"), \
             mock.patch.object(module, "send_one_job_link",
                               side_effect=fake_send_one):
            summary = module.send_to_job_links(
                f"{self.VALID_LINK},{self.SECOND_LINK}", "你好")

        self.assertEqual(summary["total"], 2)
        self.assertEqual(summary["sent"], 1)
        self.assertEqual(summary["skipped"], 1)
        self.assertEqual([r["status"] for r in summary["results"]],
                         ["skipped", "sent"])

    def test_send_batch_stops_immediately_on_risk_control(self):
        module = load_module()
        cdp = mock.Mock()
        processed = []

        def fake_send_one(cdp_arg, link, content):
            processed.append(link)
            raise module.SendRiskControlError("检测到 BOSS 风控提示：环境异常")

        with mock.patch.object(module, "CDPSession", return_value=cdp), \
             mock.patch.object(module.time, "sleep"), \
             mock.patch.object(module, "send_one_job_link",
                               side_effect=fake_send_one):
            summary = module.send_to_job_links(
                f"{self.VALID_LINK},{self.SECOND_LINK}", "你好")

        self.assertEqual(summary["aborted"], 1)
        self.assertEqual(len(processed), 1, "检测到风控后不应继续处理剩余岗位")

class ReadModeTests(unittest.TestCase):
    VALID_LINK = "https://www.zhipin.com/job_detail/abc123.html?lid=1&securityId=2"
    SECOND_LINK = "https://www.zhipin.com/job_detail/def456.html?lid=3&securityId=4"

    def _contact_state(self, **overrides):
        state = {
            "status": "招聘中",
            "buttons": [{"label": "立即沟通", "visible": True}],
            "risk_markers": [],
            "url": self.VALID_LINK,
        }
        state.update(overrides)
        return json.dumps(state, ensure_ascii=False)

    def test_read_reuses_contact_navigation_and_chat_extractor(self):
        module = load_module()
        self.assertIn("立即沟通", module.CONTACT_BUTTON_STATE_JS)
        self.assertIn("item-(?:self|myself)", module.EXTRACT_ACTIVE_INBOX_JS)
        self.assertIn("outgoing_text", module.EXTRACT_ACTIVE_INBOX_JS)
        self.assertIn("incoming_text", module.EXTRACT_ACTIVE_INBOX_JS)
        self.assertIn("message-item", module.EXTRACT_ACTIVE_INBOX_JS)
        self.assertIs(module.RiskControlError, module.SendRiskControlError)
        self.assertIs(module.JobUnavailableError, module.SendJobUnavailableError)

    def test_read_validates_links_before_cdp(self):
        module = load_module()
        with mock.patch.object(module, "CDPSession",
                               side_effect=AssertionError("不应连接 CDP")):
            with self.assertRaisesRegex(ValueError, "--job_link"):
                module.read_job_links("")
            with self.assertRaisesRegex(ValueError, "非法 JD 链接"):
                module.read_job_links("https://example.com/x.html")

    def test_read_main_rejects_missing_link(self):
        module = load_module()
        with mock.patch.object(sys, "argv", [
                "boss_cdp_raw.py", "--mode", "read",
        ]), \
             mock.patch.object(module, "require_runtime_dependencies",
                               return_value=True), \
             redirect_stdout(io.StringIO()):
            with self.assertRaises(SystemExit) as exit_context:
                module.main()
        self.assertEqual(exit_context.exception.code, 2)

    def test_read_main_requires_stdout(self):
        module = load_module()
        with mock.patch.object(sys, "argv", [
                "boss_cdp_raw.py", "--mode", "read",
                "--job_link", self.VALID_LINK,
        ]), \
             mock.patch.object(module, "require_runtime_dependencies",
                               return_value=True), \
             redirect_stdout(io.StringIO()):
            with self.assertRaises(SystemExit) as exit_context:
                module.main()
        self.assertEqual(exit_context.exception.code, 2)

    def test_read_main_stdout_emits_json(self):
        module = load_module()
        fake = {"mode": "read", "total": 1, "read": 1, "skipped": 0,
                "aborted": 0, "results": [], "read_at": "now", "scope": ""}
        with mock.patch.object(sys, "argv", [
                "boss_cdp_raw.py", "--mode", "read",
                "--job_link", self.VALID_LINK, "--stdout",
        ]), \
             mock.patch.object(module, "require_runtime_dependencies",
                               return_value=True), \
             mock.patch.object(module, "read_job_links",
                               return_value=fake), \
             redirect_stdout(io.StringIO()) as output:
            with self.assertRaises(SystemExit) as exit_context:
                module.main()
        self.assertEqual(exit_context.exception.code, 0)
        self.assertIn('"mode": "read"', output.getvalue())

    def test_read_one_job_link_reads_in_place_with_sender(self):
        module = load_module()
        cdp = mock.Mock()
        rendered = json.dumps({
            "expected_contact": "",
            "expected_contact_in_active_header": False,
            "active_header": None,
            "rendered_message_count": 2,
            "rendered_entries": [
                {"type": "incoming_text", "text": "你好，欢迎投递", "image_count": 0,
                 "link_count": 0, "class_hint": "message-item"},
                {"type": "outgoing_text", "text": "您好，我对该岗位很感兴趣", "image_count": 0,
                 "link_count": 0, "class_hint": "message-item item-myself"},
            ],
            "composer_controls": [{"tag": "contenteditable", "placeholder": ""}],
        }, ensure_ascii=False)

        def eval_js(script, sid):
            if "riskMarkers" in script and "buttons" in script:
                return self._contact_state()
            if "clicked" in script:
                return json.dumps({"clicked": "立即沟通"})
            if "has_input" in script:
                return json.dumps({"has_input": True, "tag": "contenteditable",
                                   "risk_markers": [],
                                   "url": "https://www.zhipin.com/web/geek/chat?lid=1&securityId=2"})
            if "message-item" in script:
                return rendered
            return None

        cdp.eval_js.side_effect = eval_js

        def send(method, params=None, sid=None, timeout=30):
            if method == "Target.getTargets":
                return {"result": {"targetInfos": [
                    {"targetId": "jd-tid", "type": "page", "url": self.VALID_LINK},
                ]}}
            return {"result": {}}

        cdp.send.side_effect = send
        with mock.patch.object(module, "create_page_session",
                               return_value=("jd-tid", "jd-sid")), \
             mock.patch.object(module.time, "sleep"):
            result = module.read_one_job_link(cdp, self.VALID_LINK)

        self.assertEqual(result["mode"], "read")
        self.assertEqual(result["rendered_message_count"], 2)
        senders = [m["sender"] for m in result["messages"]]
        self.assertEqual(senders, ["other", "self"])
        self.assertEqual(result["sender_counts"], {"other": 1, "self": 1})
        self.assertFalse(any(call.args[0] == "Input.insertText"
                             for call in cdp.send.call_args_list),
                         "read 模式不得发送消息")
        self.assertFalse(any(call.args[0] == "Input.dispatchKeyEvent"
                             for call in cdp.send.call_args_list))
        cdp.send.assert_any_call("Target.closeTarget", {"targetId": "jd-tid"})

    def test_read_one_job_link_attaches_popup_chat_target(self):
        module = load_module()
        cdp = mock.Mock()
        get_targets_calls = {"n": 0}
        rendered = json.dumps({
            "rendered_entries": [
                {"type": "system_event", "text": "你们已经打过招呼", "image_count": 0,
                 "link_count": 0, "class_hint": "message-item item-system"},
            ],
            "composer_controls": [],
        }, ensure_ascii=False)

        def eval_js(script, sid):
            if "riskMarkers" in script and "buttons" in script:
                return self._contact_state(buttons=[
                    {"label": "继续沟通", "visible": True}])
            if "clicked" in script:
                return json.dumps({"clicked": "继续沟通"})
            if "has_input" in script:
                if sid == "jd-sid":
                    return json.dumps({"has_input": False, "tag": "",
                                       "risk_markers": [], "url": self.VALID_LINK})
                return json.dumps({"has_input": True, "tag": "contenteditable",
                                   "risk_markers": [],
                                   "url": "https://www.zhipin.com/web/geek/chat?lid=1&securityId=2"})
            if "message-item" in script:
                return rendered
            return None

        cdp.eval_js.side_effect = eval_js

        def send(method, params=None, sid=None, timeout=30):
            if method == "Target.getTargets":
                get_targets_calls["n"] += 1
                targets = [{"targetId": "jd-tid", "type": "page", "url": self.VALID_LINK}]
                if get_targets_calls["n"] > 1:
                    targets.append({"targetId": "chat-tid", "type": "page",
                                    "url": "https://www.zhipin.com/web/geek/chat?lid=1&securityId=2"})
                return {"result": {"targetInfos": targets}}
            if method == "Target.attachToTarget":
                return {"result": {"sessionId": "chat-sid"}}
            return {"result": {}}

        cdp.send.side_effect = send
        with mock.patch.object(module, "create_page_session",
                               return_value=("jd-tid", "jd-sid")), \
             mock.patch.object(module.time, "sleep"):
            result = module.read_one_job_link(cdp, self.VALID_LINK)

        self.assertEqual(result["mode"], "read")
        self.assertEqual(
            result["conversation_url"],
            "https://www.zhipin.com/web/geek/chat?lid=1&securityId=2")
        self.assertEqual(result["message_type_counts"], {"system_event": 1})
        self.assertEqual(result["sender_counts"], {"system": 1})
        cdp.send.assert_any_call("Target.attachToTarget",
                                 {"targetId": "chat-tid", "flatten": True})
        cdp.send.assert_any_call("Target.closeTarget", {"targetId": "chat-tid"})
        cdp.send.assert_any_call("Target.closeTarget", {"targetId": "jd-tid"})

    def test_read_one_job_link_aborts_on_risk_control(self):
        module = load_module()
        cdp = mock.Mock()
        cdp.eval_js.side_effect = lambda script, sid: self._contact_state(
            status="", buttons=[], risk_markers=["环境异常"])
        with mock.patch.object(module, "create_page_session",
                               return_value=("jd-tid", "jd-sid")):
            with self.assertRaisesRegex(module.RiskControlError, "环境异常"):
                module.read_one_job_link(cdp, self.VALID_LINK)

    def test_read_one_job_link_skips_closed_job(self):
        module = load_module()
        cdp = mock.Mock()
        cdp.eval_js.side_effect = lambda script, sid: self._contact_state(
            status="已关闭")
        with mock.patch.object(module, "create_page_session",
                               return_value=("jd-tid", "jd-sid")):
            with self.assertRaisesRegex(module.JobUnavailableError, "已关闭"):
                module.read_one_job_link(cdp, self.VALID_LINK)

    def test_read_batch_skips_unavailable_and_counts(self):
        module = load_module()
        cdp = mock.Mock()

        def fake_read_one(cdp_arg, link, max_entries=200, result_mode="read"):
            if link == self.VALID_LINK:
                raise module.JobUnavailableError("职位已关闭，跳过")
            return {"rendered_message_count": 3, "messages": [],
                    "scope": "", "message_type_counts": {}, "sender_counts": {}}

        with mock.patch.object(module, "CDPSession", return_value=cdp), \
             mock.patch.object(module.time, "sleep"), \
             mock.patch.object(module, "read_one_job_link",
                               side_effect=fake_read_one):
            summary = module.read_job_links(
                f"{self.VALID_LINK},{self.SECOND_LINK}")

        self.assertEqual(summary["total"], 2)
        self.assertEqual(summary["read"], 1)
        self.assertEqual(summary["skipped"], 1)
        self.assertEqual([r["status"] for r in summary["results"]],
                         ["skipped", "read"])

    def test_read_batch_stops_immediately_on_risk_control(self):
        module = load_module()
        cdp = mock.Mock()
        processed = []

        def fake_read_one(cdp_arg, link, max_entries=200, result_mode="read"):
            processed.append(link)
            raise module.RiskControlError("检测到 BOSS 风控提示：环境异常")

        with mock.patch.object(module, "CDPSession", return_value=cdp), \
             mock.patch.object(module.time, "sleep"), \
             mock.patch.object(module, "read_one_job_link",
                               side_effect=fake_read_one):
            summary = module.read_job_links(
                f"{self.VALID_LINK},{self.SECOND_LINK}")

        self.assertEqual(summary["aborted"], 1)
        self.assertEqual(len(processed), 1, "检测到风控后不应继续处理剩余岗位")


class ChatReadAndSendVerificationTests(unittest.TestCase):
    VALID_LINK = ("https://www.zhipin.com/job_detail/"
                  "52b2610d4a110fa70nF62NW1EVJQ.html?securityId=abcd~~")
    SECOND_LINK = ("https://www.zhipin.com/job_detail/"
                   "another0nF62NW1EVJQ.html?securityId=efgh~~")

    def _contact_state(self, **overrides):
        state = {
            "status": "招聘中",
            "buttons": [{"label": "立即沟通", "visible": True}],
            "risk_markers": [],
            "url": self.VALID_LINK,
        }
        state.update(overrides)
        return json.dumps(state, ensure_ascii=False)


    def test_strip_boss_time_prefix_strips_delivery_prefix(self):
        module = load_module()
        strip = module._strip_boss_time_prefix
        self.assertEqual(strip("12:45 送达 你好"), "你好")
        self.assertEqual(strip("2026-08-18 12:45 送达 你好"), "你好")
        self.assertEqual(strip("送达 你好"), "你好")
        self.assertEqual(strip("你好"), "你好")
        self.assertEqual(strip(""), "")

    def test_readback_matches_detects_sent_content(self):
        module = load_module()
        row = {"type": "outgoing_text", "text": "12:45 送达 你好，我是候选人"}
        self.assertTrue(module._readback_matches(row, "你好，我是候选人"))
        self.assertFalse(module._readback_matches(row, "完全不同的内容"))
        self.assertFalse(module._readback_matches({}, "你好"))
        self.assertFalse(module._readback_matches(None, "你好"))
        self.assertFalse(module._readback_matches(
            {"type": "outgoing_text", "text": "12:45 送达 你好"}, ""))

    def test_sidebar_extract_js_covers_name_status_and_time(self):
        module = load_module()
        self.assertIn(".friend-content", module.EXTRACT_SIDEBAR_CONVERSATIONS_JS)
        self.assertIn(".name-text", module.EXTRACT_SIDEBAR_CONVERSATIONS_JS)
        self.assertIn(".message-status", module.EXTRACT_SIDEBAR_CONVERSATIONS_JS)
        self.assertIn("status-read", module.EXTRACT_SIDEBAR_CONVERSATIONS_JS)
        self.assertIn("status-delivery", module.EXTRACT_SIDEBAR_CONVERSATIONS_JS)
        self.assertIn(".last-msg-text", module.EXTRACT_SIDEBAR_CONVERSATIONS_JS)
        self.assertIn("__INDEX__", module.CLICK_SIDEBAR_ITEM_JS)
        self.assertIn(".friend-content-warp", module.CLICK_SIDEBAR_ITEM_JS)

    def test_click_sidebar_conversation_ok_and_invalid(self):
        module = load_module()
        cdp = mock.Mock()
        cdp.eval_js.return_value = json.dumps(
            {"ok": True, "index": 2, "count": 5, "x": 301, "y": 206})
        self.assertEqual(module._click_sidebar_conversation(cdp, "sid", 2), 5)
        mouse_calls = [call for call in cdp.send.call_args_list
                       if call.args[0] == "Input.dispatchMouseEvent"]
        self.assertEqual(len(mouse_calls), 2)
        self.assertEqual(mouse_calls[0].args[1]["type"], "mousePressed")
        self.assertEqual(mouse_calls[1].args[1]["type"], "mouseReleased")
        self.assertEqual(mouse_calls[0].args[1]["x"], 301)
        self.assertEqual(mouse_calls[0].args[1]["y"], 206)
        cdp.eval_js.return_value = json.dumps({"ok": False, "index": 9, "count": 3})
        with self.assertRaisesRegex(RuntimeError, "序号 9 无效"):
            module._click_sidebar_conversation(cdp, "sid", 9)

    def test_read_open_chat_conversation_reads_current_selection(self):
        module = load_module()
        cdp = mock.Mock()

        def send(method, params=None, sid=None, timeout=30):
            if method == "Target.getTargets":
                return {"result": {"targetInfos": [{
                    "targetId": "chat-target", "type": "page",
                    "url": "https://www.zhipin.com/web/geek/chat",
                }]}}
            if method == "Target.attachToTarget":
                return {"result": {"sessionId": "chat-session"}}
            return {"result": {}}

        cdp.send.side_effect = send
        cdp.eval_js.side_effect = self._chat_payload

        with mock.patch.object(module, "CDPSession", return_value=cdp):
            result = module.read_open_chat_conversation("刘姗", max_entries=10)

        self.assertEqual(result["mode"], "read-chat")
        self.assertEqual(result["rendered_message_count"], 3)
        self.assertEqual(result["sender_counts"], {"self": 1, "other": 1, "system": 1})
        self.assertEqual(result["messages"][0]["sender"], "other")
        self.assertEqual(result["messages"][1]["sender"], "self")
        calls = [call.args[0] for call in cdp.send.call_args_list]
        self.assertNotIn("Page.navigate", calls)
        self.assertFalse(any(method.startswith("Input.") for method in calls))

    def _chat_payload(self, script, sid):
        if "message-item" not in script:
            return None
        return json.dumps({
            "expected_contact_in_active_header": True,
            "active_header": {"text": "刘姗｜HR"},
            "rendered_entries": [
                {"type": "incoming_text", "text": "你好，看到你投递了 XX 岗",
                 "image_count": 0, "link_count": 0, "class_hint": "message-item"},
                {"type": "outgoing_text", "text": "12:45 送达 您好，我对该岗位很感兴趣",
                 "image_count": 0, "link_count": 0, "class_hint": "message-item item-myself"},
                {"type": "system_event", "text": "", "image_count": 0, "link_count": 0,
                 "class_hint": "message-item item-system"},
            ],
            "composer_controls": [],
        }, ensure_ascii=False)

    def test_read_open_chat_conversation_verifies_expected_contact(self):
        module = load_module()
        cdp = mock.Mock()

        def send(method, params=None, sid=None, timeout=30):
            if method == "Target.getTargets":
                return {"result": {"targetInfos": [{
                    "targetId": "chat-target", "type": "page",
                    "url": "https://www.zhipin.com/web/geek/chat",
                }]}}
            if method == "Target.attachToTarget":
                return {"result": {"sessionId": "chat-session"}}
            return {"result": {}}

        cdp.send.side_effect = send
        cdp.eval_js.side_effect = lambda script, sid: json.dumps({
            "expected_contact_in_active_header": False,
            "active_header": {"text": "其他人｜HR"},
            "rendered_entries": [],
            "composer_controls": [],
        }, ensure_ascii=False)

        with mock.patch.object(module, "CDPSession", return_value=cdp):
            with self.assertRaisesRegex(RuntimeError, "未显示"):
                module.read_open_chat_conversation("刘姗")

    def test_switch_and_read_conversations_clicks_sidebar_indices(self):
        module = load_module()
        cdp = mock.Mock()
        clicked = []
        cdp.eval_js.side_effect = self._chat_payload

        def click_side(cdp_arg, sid, index):
            clicked.append(index)
            return 3

        with mock.patch.object(module, "CDPSession", return_value=cdp), \
             mock.patch.object(module, "attach_active_inbox_target",
                               return_value=("chat-target", "chat-session")), \
             mock.patch.object(module, "_chat_page_risk_check"), \
             mock.patch.object(module, "_click_sidebar_conversation",
                               side_effect=click_side), \
             mock.patch.object(module.time, "sleep"):
            result = module.switch_and_read_conversations("0,2", max_entries=10)

        self.assertEqual(result["mode"], "read-chat-switch")
        self.assertEqual(clicked, [0, 2])
        self.assertEqual(result["total"], 2)
        self.assertEqual(result["read"], 2)

    def test_switch_and_read_rejects_invalid_index(self):
        module = load_module()
        with self.assertRaisesRegex(ValueError, "非法的会话序号"):
            module.switch_and_read_conversations("abc")
        with self.assertRaisesRegex(ValueError, "--switch-index"):
            module.switch_and_read_conversations("")

    def test_list_chat_conversations_merges_native_and_sidebar(self):
        module = load_module()
        cdp = mock.Mock()
        cdp.send.side_effect = lambda method, params=None, sid=None, timeout=30: {"result": {}}
        native = [{
            "name": "刘姗", "brandName": "旧公司", "title": "招聘经理",
            "avatar": "https://img.example/avatar.png",
            "encryptUid": "uid-1", "encryptJobId": "job-1",
            "sourceTitle": "Agent开发", "securityId": "s1",
            "unreadMsgCount": 2, "lastTime": "13:00", "lastTS": 123,
            "isTop": False, "chatStatus": 0, "relationType": 2,
            "uid": 90001,
            "lastMessageInfo": {
                "fromId": 204016845, "toId": 90001, "status": 2,
                "showText": "13:00 已读 你好", "msgTime": 1000,
            },
        }]
        cdp.eval_js.side_effect = lambda script, sid: (
            json.dumps([{
                "index": 0, "name": "刘姗", "company": "新公司", "title": "招聘经理",
                "read_status": "已读", "last_time": "13:02",
                "last_message_preview": "你好", "selected": True,
                "avatar": "https://img.example/side.png",
            }], ensure_ascii=False)
            if ".friend-content" in script else None
        )

        with mock.patch.object(module, "CDPSession", return_value=cdp), \
             mock.patch.object(module, "create_page_session",
                               return_value=("tid", "sid")), \
             mock.patch.object(module, "wait_for_native_inbox_list",
                               return_value=native), \
             mock.patch.object(module, "_chat_page_risk_check"), \
             mock.patch.object(module.time, "sleep"):
            result = module.list_chat_conversations()

        self.assertEqual(result["mode"], "read-list")
        self.assertEqual(result["conversation_total"], 1)
        self.assertEqual(result["unread_total"], 2)
        row = result["conversations"][0]
        self.assertEqual(row["index"], 0)
        self.assertEqual(row["recruiter_name"], "刘姗")
        self.assertEqual(row["company"], "新公司")
        self.assertEqual(row["recruiter_title"], "招聘经理")
        self.assertEqual(row["recruiter_avatar"], "https://img.example/side.png")
        self.assertEqual(row["read_status"], "已读")
        self.assertEqual(row["last_time"], "13:02")
        self.assertTrue(row["selected"])
        self.assertIn("job_detail/job-1", row["job_link"])
        self.assertEqual(row["unread_count"], 2)
        self.assertEqual(row["last_message_sender"], "self")
        self.assertEqual(row["last_message_read"], "已读")
        self.assertEqual(row["last_message_native_status"], 2)
        self.assertEqual(row["last_message_text"], "13:00 已读 你好")

    def test_last_message_info_derives_sender_and_read_state(self):
        module = load_module()
        self.assertEqual(module._last_message_info({
            "uid": 90001,
            "lastMessageInfo": {"fromId": 204016845, "status": 2},
        })["sender"], "self")
        self.assertEqual(module._last_message_info({
            "uid": 90001,
            "lastMessageInfo": {"fromId": 90001, "status": 2},
        })["sender"], "other")
        self.assertEqual(module._last_message_info({
            "uid": 90001,
            "lastMessageInfo": {"fromId": 90001, "status": 1},
        })["read_state"], "送达")
        self.assertEqual(module._last_message_info({
            "uid": 90001,
            "lastMessageInfo": {"fromId": 90001, "status": 0},
        })["read_state"], "未读")
        self.assertEqual(module._last_message_info({
            "uid": 90001,
        })["sender"], "unknown")
        self.assertEqual(module._last_message_info({
            "uid": 90001,
            "lastMessageInfo": {"fromId": 90001, "status": 2, "showText": "文本"},
        })["text"], "文本")

    def test_match_sidebar_index_by_name_with_position_fallback(self):
        module = load_module()
        native = [
            {"job_id": "job-a", "recruiter_name": "吴安琪", "company": "阿里", "filtered": False},
            {"job_id": "job-b", "recruiter_name": "陈女士", "company": "算秩", "filtered": False},
            {"job_id": "job-c", "recruiter_name": "张先生", "company": "均阳", "filtered": True},
            {"job_id": "job-d", "recruiter_name": "闫朝伟", "company": "阿里", "filtered": False},
        ]
        sidebar = [
            {"index": 0, "name": "吴安琪", "company": "阿里"},
            {"index": 1, "name": "陈女士", "company": "算秩"},
            {"index": 2, "name": "闫朝伟", "company": "阿里"},
        ]
        self.assertEqual(module._match_sidebar_index(native, sidebar, "job-a"), 0)
        self.assertEqual(module._match_sidebar_index(native, sidebar, "job-b"), 1)
        # 被过滤项不在侧边栏，name 匹配失败时按非过滤顺序回退
        self.assertEqual(module._match_sidebar_index(native, sidebar, "job-d"), 2)
        self.assertIsNone(module._match_sidebar_index(native, sidebar, "job-none"))

    def test_match_sidebar_index_accepts_raw_native_keys(self):
        module = load_module()
        native = [
            {"encryptJobId": "job-a", "name": "吴安琪", "brandName": "阿里", "filtered": False},
            {"encryptJobId": "job-b", "name": "陈女士", "brandName": "算秩", "filtered": False},
            {"encryptJobId": "job-c", "name": "张先生", "brandName": "均阳", "filtered": True},
        ]
        sidebar = [
            {"index": 0, "name": "吴安琪", "company": "阿里"},
            {"index": 1, "name": "陈女士", "company": "算秩"},
        ]
        self.assertEqual(module._match_sidebar_index(native, sidebar, "job-a"), 0)
        self.assertEqual(module._match_sidebar_index(native, sidebar, "job-b"), 1)
        self.assertIsNone(module._match_sidebar_index(native, sidebar, "job-c"))

    def test_match_sidebar_index_disables_position_fallback_when_sidebar_missing_rows(self):
        module = load_module()
        # 侧边栏渲染了 14 行，但 native 有 15 项（张先生 relationType=5 未渲染）：
        # 位置回退会点错人，必须返回 None。
        native = [
            {"encryptJobId": "job-a", "name": "吴安琪", "brandName": "阿里", "filtered": False},
            {"encryptJobId": "job-hidden", "name": "张先生", "brandName": "均阳", "filtered": False},
            {"encryptJobId": "job-b", "name": "闫朝伟", "brandName": "阿里", "filtered": False},
        ]
        sidebar = [
            {"index": 0, "name": "吴安琪", "company": "阿里"},
            {"index": 1, "name": "闫朝伟", "company": "阿里"},
        ]
        self.assertEqual(module._match_sidebar_index(native, sidebar, "job-a"), 0)
        self.assertEqual(module._match_sidebar_index(native, sidebar, "job-b"), 1)
        # 未渲染会话：位置回退被禁用，返回 None（上层回退 job_link）
        self.assertIsNone(module._match_sidebar_index(native, sidebar, "job-hidden"))

    def test_list_chat_conversations_marks_unrendered_rows(self):
        module = load_module()
        cdp = mock.Mock()
        cdp.send.side_effect = lambda method, params=None, sid=None, timeout=30: {"result": {}}
        native = [
            {
                "name": "吴安琪", "brandName": "阿里", "title": "招聘者",
                "encryptUid": "uid-0", "encryptJobId": "job-0",
                "unreadMsgCount": 0, "lastTime": "13:00", "lastTS": 1,
                "uid": 90001, "filtered": False,
                "lastMessageInfo": {"fromId": 90001, "status": 1, "showText": "你好", "msgTime": 1},
            },
            {
                "name": "张先生", "brandName": "均阳", "title": "人事主管",
                "encryptUid": "uid-1", "encryptJobId": "job-1",
                "unreadMsgCount": 0, "lastTime": "昨天", "lastTS": 2,
                "uid": 563895339, "filtered": False, "relationType": 5,
                "lastMessageInfo": {"fromId": 563895339, "status": 0, "showText": "", "msgTime": 2},
            },
        ]
        cdp.eval_js.side_effect = lambda script, sid: (
            json.dumps([{
                "index": 0, "name": "吴安琪", "company": "阿里", "title": "招聘者",
                "read_status": "送达", "last_time": "13:00",
                "last_message_preview": "你好", "selected": False, "avatar": "",
            }], ensure_ascii=False)
            if ".friend-content" in script else None
        )
        with mock.patch.object(module, "CDPSession", return_value=cdp),              mock.patch.object(module, "create_page_session",
                               return_value=("tid", "sid")),              mock.patch.object(module, "wait_for_native_inbox_list",
                               return_value=native),              mock.patch.object(module, "_chat_page_risk_check"),              mock.patch.object(module.time, "sleep"):
            result = module.list_chat_conversations()
        rendered = [r for r in result["conversations"] if r["rendered"]]
        unrendered = [r for r in result["conversations"] if not r["rendered"]]
        self.assertEqual(len(rendered), 1)
        self.assertEqual(rendered[0]["index"], 0)
        self.assertEqual(rendered[0]["recruiter_name"], "吴安琪")
        self.assertEqual(len(unrendered), 1)
        self.assertEqual(unrendered[0]["index"], None)
        self.assertEqual(unrendered[0]["recruiter_name"], "张先生")
        self.assertEqual(unrendered[0]["last_message_sender"], "other")

    def test_attach_active_inbox_target_picks_first_of_many(self):
        module = load_module()
        cdp = mock.Mock()

        def fake_send(method, params=None, sid=None, timeout=30):
            if method == "Target.getTargets":
                return {"result": {"targetInfos": [
                    {"type": "page", "url": "https://www.zhipin.com/web/geek/chat", "targetId": "t1"},
                    {"type": "page", "url": "https://www.zhipin.com/web/geek/chat", "targetId": "t2"},
                ]}}
            if method == "Target.attachToTarget":
                return {"result": {"sessionId": "sess"}}
            return {"result": {}}

        cdp.send.side_effect = fake_send
        tid, sid = module.attach_active_inbox_target(cdp)
        self.assertEqual(tid, "t1")
        self.assertEqual(sid, "sess")

    def test_attach_active_inbox_target_raises_without_chat_page(self):
        module = load_module()
        cdp = mock.Mock()
        cdp.send.return_value = {"result": {"targetInfos": [
            {"type": "page", "url": "https://www.zhipin.com/job_detail/x.html", "targetId": "t1"},
        ]}}
        with self.assertRaisesRegex(RuntimeError, "未找到已打开的 BOSS 消息页"):
            module.attach_active_inbox_target(cdp)

    def test_list_chat_conversations_rejects_bad_inbox_url(self):
        module = load_module()
        with self.assertRaisesRegex(ValueError, "--inbox-url"):
            module.list_chat_conversations("https://example.com/x")

    def test_read_main_list_dispatches_to_list_chat_conversations(self):
        module = load_module()
        fake = {"mode": "read-list", "conversation_total": 1, "conversations": [],
                "unread_total": 0, "inbox_url": "", "scope": "", "listed_at": "now"}
        with mock.patch.object(sys, "argv", [
                "boss_cdp_raw.py", "--mode", "read", "--list", "--stdout",
        ]), \
             mock.patch.object(module, "require_runtime_dependencies",
                               return_value=True), \
             mock.patch.object(module, "list_chat_conversations",
                               return_value=fake), \
             redirect_stdout(io.StringIO()) as output:
            with self.assertRaises(SystemExit) as exit_context:
                module.main()
        self.assertEqual(exit_context.exception.code, 0)
        self.assertIn('"mode": "read-list"', output.getvalue())

    def test_read_main_chat_switch_dispatches_to_switch_and_read(self):
        module = load_module()
        fake = {"mode": "read-chat-switch", "total": 1, "read": 1,
                "skipped": 0, "results": [], "scope": "", "read_at": "now"}
        with mock.patch.object(sys, "argv", [
                "boss_cdp_raw.py", "--mode", "read", "--chat",
                "--switch-index", "0", "--stdout",
        ]), \
             mock.patch.object(module, "require_runtime_dependencies",
                               return_value=True), \
             mock.patch.object(module, "switch_and_read_conversations",
                               return_value=fake), \
             redirect_stdout(io.StringIO()) as output:
            with self.assertRaises(SystemExit) as exit_context:
                module.main()
        self.assertEqual(exit_context.exception.code, 0)
        self.assertIn('"mode": "read-chat-switch"', output.getvalue())

    def test_read_main_chat_job_link_dispatches_to_switch_or_open(self):
        module = load_module()
        fake = {"mode": "read-chat", "total": 1, "read": 1, "skipped": 0,
                "aborted": 0, "via_sidebar": 1, "via_job_link": 0,
                "results": [], "read_at": "now", "scope": ""}
        with mock.patch.object(sys, "argv", [
                "boss_cdp_raw.py", "--mode", "read", "--chat",
                "--job_link", self.VALID_LINK, "--stdout",
        ]), \
             mock.patch.object(module, "require_runtime_dependencies",
                               return_value=True), \
             mock.patch.object(module, "read_chat_switch_or_open",
                               return_value=fake) as switch_or_open, \
             redirect_stdout(io.StringIO()) as output:
            with self.assertRaises(SystemExit) as exit_context:
                module.main()
        self.assertEqual(exit_context.exception.code, 0)
        self.assertEqual(switch_or_open.call_args.args[0], self.VALID_LINK)
        self.assertIn('"mode": "read-chat"', output.getvalue())

    def test_read_main_chat_current_dispatches_to_open_chat(self):
        module = load_module()
        fake = {"mode": "read-chat", "rendered_message_count": 0, "messages": [],
                "message_type_counts": {}, "sender_counts": {}, "scope": "",
                "composer_controls": [], "scraped_at": "now"}
        with mock.patch.object(sys, "argv", [
                "boss_cdp_raw.py", "--mode", "read", "--chat", "--stdout",
        ]), \
             mock.patch.object(module, "require_runtime_dependencies",
                               return_value=True), \
             mock.patch.object(module, "read_open_chat_conversation",
                               return_value=fake), \
             redirect_stdout(io.StringIO()) as output:
            with self.assertRaises(SystemExit) as exit_context:
                module.main()
        self.assertEqual(exit_context.exception.code, 0)
        self.assertIn('"mode": "read-chat"', output.getvalue())

    def test_read_main_list_rejects_missing_stdout(self):
        module = load_module()
        with mock.patch.object(sys, "argv", [
                "boss_cdp_raw.py", "--mode", "read", "--list",
        ]), \
             mock.patch.object(module, "require_runtime_dependencies",
                               return_value=True), \
             redirect_stdout(io.StringIO()):
            with self.assertRaises(SystemExit) as exit_context:
                module.main()
        self.assertEqual(exit_context.exception.code, 2)

    def test_send_one_job_link_reports_readback_verification(self):
        module = load_module()
        cdp = mock.Mock()
        outgoing = {"count": 0}
        composer = {"text": ""}
        rendered = {"entries": []}

        def eval_js(script, sid):
            if "composer_text" in script:
                return composer["text"]
            if "riskMarkers" in script and "buttons" in script:
                return self._contact_state()
            if "clicked" in script:
                return json.dumps({"clicked": "立即沟通"})
            if "has_input" in script:
                return json.dumps({"has_input": True, "tag": "contenteditable",
                                   "risk_markers": [], "url": "https://www.zhipin.com/web/geek/chat"})
            if "el.focus" in script:
                return json.dumps({"ok": True, "tag": "contenteditable"})
            if "message-item" in script:
                return json.dumps({
                    "active_header": {"text": "刘姗｜HR"},
                    "rendered_entries": rendered["entries"],
                    "composer_controls": [],
                }, ensure_ascii=False)
            return None

        cdp.eval_js.side_effect = eval_js

        def send(method, params=None, sid=None, timeout=30):
            if method == "Input.insertText":
                composer["text"] += (params or {}).get("text", "")
                return {"result": {}}
            if method == "Input.dispatchKeyEvent":
                composer["text"] = ""
                return {"result": {}}
            if method == "Target.getTargets":
                return {"result": {"targetInfos": [
                    {"targetId": "jd-tid", "type": "page", "url": self.VALID_LINK},
                ]}}
            if method == "DOM.getDocument":
                return {"result": {"root": {"nodeId": 1}}}
            if method == "DOM.querySelector":
                return {"result": {"nodeId": 2}}
            return {"result": {}}

        cdp.send.side_effect = send
        with mock.patch.object(module, "create_page_session",
                               return_value=("jd-tid", "jd-sid")), \
             mock.patch.object(module, "_dispatch_enter"), \
             mock.patch.object(module, "count_active_outgoing_text",
                               return_value=0), \
             mock.patch.object(module, "_read_composer_text",
                               side_effect=lambda cdp_arg, sid_arg: composer["text"]), \
             mock.patch.object(module.time, "sleep"):
            # 先模拟发送完成后历史里出现我们的消息
            def mark_sent(*args):
                rendered["entries"] = [
                    {"type": "outgoing_text", "text": "12:45 送达 你好",
                     "image_count": 0, "link_count": 0, "class_hint": "message-item"},
                ]
            cdp.eval_js.side_effect = eval_js
            module._dispatch_enter.side_effect = mark_sent
            result = module.send_one_job_link(cdp, self.VALID_LINK, "你好")

        self.assertTrue(result["submitted"])
        self.assertTrue(result["send_success"])
        self.assertEqual(result["verified_last_sender"], "self")
        self.assertEqual(result["verified_last_text"], "12:45 送达 你好")
        # Enter 被 mock 为只触发回读数据填充，不会真正清空输入框
        self.assertFalse(result["composer_cleared_after_send"])

    def test_send_to_job_links_summary_counts_verified(self):
        module = load_module()
        fake = {
            "mode": "send", "job_link": self.VALID_LINK, "message": "你好",
            "submitted": True, "send_success": True,
            "verified_last_sender": "self", "verified_last_text": "你好",
            "post_send_visible": True, "composer_cleared_after_send": True,
            "sent_at": "now",
        }
        cdp = mock.Mock()
        with mock.patch.object(module, "CDPSession", return_value=cdp), \
             mock.patch.object(module.time, "sleep"), \
             mock.patch.object(module, "send_one_job_link",
                               return_value=fake):
            summary = module.send_to_job_links(self.VALID_LINK, "你好")
        self.assertEqual(summary["sent"], 1)
        self.assertEqual(summary["sent_verified"], 1)


    def test_read_one_chat_prefer_sidebar_switches_when_found(self):
        module = load_module()
        cdp = mock.Mock()
        cdp.eval_js.side_effect = lambda script, sid: (
            json.dumps([{
                "index": 3, "name": "刘姗", "company": "新公司", "title": "HR",
                "read_status": "已读", "last_time": "13:02",
                "last_message_preview": "你好", "selected": False,
                "avatar": "",
            }], ensure_ascii=False)
            if ".friend-content" in script else None
        )

        with mock.patch.object(module, "attach_active_inbox_target",
                               return_value=("chat-target", "chat-session")), \
             mock.patch.object(module, "_chat_page_risk_check"), \
             mock.patch.object(module, "_capture_native_items_temp",
                               return_value=[{
                                   "job_id": "52b2610d4a110fa70nF62NW1EVJQ",
                                   "recruiter_name": "刘姗", "company": "新公司",
                                   "filtered": False,
                               }]), \
             mock.patch.object(module, "_click_sidebar_conversation",
                               return_value=4) as click_side, \
             mock.patch.object(module, "_read_chat_payload",
                               return_value={
                                   "rendered_entries": [
                                       {"type": "incoming_text", "text": "你好",
                                        "image_count": 0, "link_count": 0,
                                        "class_hint": "message-item"},
                                   ],
                               }), \
             mock.patch.object(module.time, "sleep"):
            result, via = module._read_one_chat_prefer_sidebar(
                cdp, self.VALID_LINK, "52b2610d4a110fa70nF62NW1EVJQ", 10)

        self.assertEqual(via, "sidebar")
        self.assertEqual(result["switch_index"], 3)
        self.assertEqual(result["rendered_message_count"], 1)
        click_side.assert_called_once_with(cdp, "chat-session", 3)

    def test_read_one_chat_prefer_sidebar_falls_back_to_job_link(self):
        module = load_module()
        cdp = mock.Mock()
        fake_read = {"mode": "read-chat", "rendered_message_count": 0,
                     "messages": [], "message_type_counts": {},
                     "sender_counts": {}, "scope": "", "read_at": "now"}

        with mock.patch.object(module, "attach_active_inbox_target",
                               side_effect=RuntimeError("未找到唯一消息页")), \
             mock.patch.object(module, "read_one_job_link",
                               return_value=fake_read) as read_one:
            result, via = module._read_one_chat_prefer_sidebar(
                cdp, self.VALID_LINK, "52b2610d4a110fa70nF62NW1EVJQ", 10)

        self.assertEqual(via, "job_link")
        self.assertEqual(read_one.call_args.args[1], self.VALID_LINK)
        self.assertEqual(result["mode"], "read-chat")

    def test_read_chat_switch_or_open_prefers_sidebar_and_reports_via(self):
        module = load_module()
        cdp = mock.Mock()
        fake_sidebar = {
            "mode": "read-chat", "switch_index": 0,
            "rendered_message_count": 2, "messages": [],
            "message_type_counts": {}, "sender_counts": {},
            "scope": "", "read_at": "now",
        }
        with mock.patch.object(module, "CDPSession", return_value=cdp), \
             mock.patch.object(module.time, "sleep"), \
             mock.patch.object(module, "_read_one_chat_prefer_sidebar",
                               return_value=(fake_sidebar, "sidebar")):
            summary = module.read_chat_switch_or_open(self.VALID_LINK)

        self.assertEqual(summary["mode"], "read-chat")
        self.assertEqual(summary["read"], 1)
        self.assertEqual(summary["via_sidebar"], 1)
        self.assertEqual(summary["via_job_link"], 0)
        self.assertEqual(summary["results"][0]["entered_via"], "sidebar")

    def test_read_chat_switch_or_open_reports_job_link_fallback(self):
        module = load_module()
        cdp = mock.Mock()
        fake_read = {
            "mode": "read-chat", "rendered_message_count": 3, "messages": [],
            "message_type_counts": {}, "sender_counts": {},
            "scope": "", "read_at": "now",
        }
        with mock.patch.object(module, "CDPSession", return_value=cdp), \
             mock.patch.object(module.time, "sleep"), \
             mock.patch.object(module, "_read_one_chat_prefer_sidebar",
                               return_value=(fake_read, "job_link")):
            summary = module.read_chat_switch_or_open(self.VALID_LINK)

        self.assertEqual(summary["via_sidebar"], 0)
        self.assertEqual(summary["via_job_link"], 1)
        self.assertEqual(summary["results"][0]["entered_via"], "job_link")

    def test_read_chat_switch_or_open_aborts_on_risk_control(self):
        module = load_module()
        cdp = mock.Mock()
        processed = []

        def fake_prefer(cdp_arg, link, job_id, max_entries):
            processed.append(link)
            raise module.RiskControlError("检测到 BOSS 风控提示：环境异常")

        with mock.patch.object(module, "CDPSession", return_value=cdp), \
             mock.patch.object(module.time, "sleep"), \
             mock.patch.object(module, "_read_one_chat_prefer_sidebar",
                               side_effect=fake_prefer):
            summary = module.read_chat_switch_or_open(
                f"{self.VALID_LINK},{self.SECOND_LINK}")

        self.assertEqual(summary["aborted"], 1)
        self.assertEqual(len(processed), 1)


if __name__ == "__main__":
    unittest.main()
