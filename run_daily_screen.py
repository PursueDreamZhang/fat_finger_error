from __future__ import annotations

import argparse

from src.daily_screen.pipeline import analyze_commodities


def main() -> None:
    parser = argparse.ArgumentParser(description="期货乌龙指日线初筛")
    parser.add_argument("--symbols", required=True, help="品种编码，多个用逗号分隔，例如 AU,JD")
    parser.add_argument("--start-date", required=True, help="开始日期，格式 YYYYMMDD")
    parser.add_argument("--end-date", required=True, help="结束日期，格式 YYYYMMDD")
    parser.add_argument("--output-dir", required=False, help="输出目录")
    args = parser.parse_args()

    result = analyze_commodities(
        symbols=args.symbols,
        start_date=args.start_date,
        end_date=args.end_date,
        output_dir=args.output_dir,
    )
    print(result["html_report_path"])


if __name__ == "__main__":
    main()
