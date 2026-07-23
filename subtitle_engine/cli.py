import argparse
import pathlib
import sys
from subtitle_engine.processor import SubtitleProcessor


def main():
    parser = argparse.ArgumentParser(description="Taka Subtitle Engine CLI")
    subparsers = parser.add_subparsers(dest="command", help="Subtitle Engine Commands")

    # Command: process (burn subtitles into video)
    proc_parser = subparsers.add_parser("process", help="Generate and burn subtitles onto video")
    proc_parser.add_argument("--video", required=True, help="Input video or audio file path")
    proc_parser.add_argument("--transcript", help="Original script/transcript file path or text")
    proc_parser.add_argument("--preset", default="viral-bold-yellow", help="Preset name or json file path")
    proc_parser.add_argument("--output", required=True, help="Output video file path")

    # Command: generate-ass (only produce ASS file)
    ass_parser = subparsers.add_parser("generate-ass", help="Generate ASS subtitle file only")
    ass_parser.add_argument("--video", required=True, help="Input video or audio file path")
    ass_parser.add_argument("--transcript", help="Original script/transcript file path or text")
    ass_parser.add_argument("--preset", default="viral-bold-yellow", help="Preset name or json file path")
    ass_parser.add_argument("--output", help="Output ASS file path")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(1)

    # Read transcript if file path provided
    transcript_text = None
    if args.transcript:
        t_path = pathlib.Path(args.transcript)
        if t_path.exists() and t_path.is_file():
            try:
                transcript_text = t_path.read_text(encoding="utf-8")
            except Exception as e:
                print(f"[CLI Error] Failed to read transcript file: {e}")
                sys.exit(1)
        else:
            transcript_text = args.transcript

    processor = SubtitleProcessor(preset_path_or_id=args.preset)

    if args.command == "generate-ass":
        out_ass = processor.process_and_render_ass(
            audio_or_video_path=pathlib.Path(args.video),
            transcript=transcript_text,
            output_ass_path=pathlib.Path(args.output) if args.output else None
        )
        print(f"✨ Successfully generated ASS subtitles: {out_ass}")

    elif args.command == "process":
        out_vid = processor.burn_subtitles_to_video(
            input_video_path=pathlib.Path(args.video),
            output_video_path=pathlib.Path(args.output),
            transcript=transcript_text
        )
        print(f"🚀 Successfully processed video with burned subtitles: {out_vid}")


if __name__ == "__main__":
    main()
