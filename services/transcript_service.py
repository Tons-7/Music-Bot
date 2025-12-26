# import asyncio
# from concurrent.futures import ThreadPoolExecutor
# from typing import Optional
#
# from youtube_transcript_api import YouTubeTranscriptApi
# from youtube_transcript_api._errors import TranscriptsDisabled, NoTranscriptFound
# import re
#
# from transformers import AutoTokenizer, AutoModelForSeq2SeqLM
# import torch
#
# import logging
#
# logger = logging.getLogger(__name__)
#
# executor = ThreadPoolExecutor(max_workers=1)
#
#
# class TranscriptService:
#     def __init__(self, bot):
#         self.bot = bot
#         self.summarizer = None
#         self.tokenizer = None
#
#     def _load_summarizer(self):
#         if self.summarizer is None:
#             logger.info("Loading Flan-T5-Base model...")
#             model_name = "google/flan-t5-base"
#             self.tokenizer = AutoTokenizer.from_pretrained(model_name)
#             self.summarizer = AutoModelForSeq2SeqLM.from_pretrained(
#                 model_name,
#                 torch_dtype=torch.float16
#             )
#             device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
#             self.summarizer.to(device)
#             logger.info(f"Flan-T5-Base loaded on {device}")
#         return self.summarizer
#
#     @staticmethod
#     def extract_youtube_video_id(url: str) -> Optional[str]:
#         patterns = [
#             r'(?:https?://)?(?:www\.)?youtube\.com/watch\?v=([a-zA-Z0-9_-]{11})',
#             r'(?:https?://)?youtu\.be/([a-zA-Z0-9_-]{11})',
#             r'(?:https?://)?(?:www\.)?youtube\.com/embed/([a-zA-Z0-9_-]{11})',
#         ]
#
#         for pattern in patterns:
#             match = re.search(pattern, url)
#             if match:
#                 return match.group(1)
#
#         return None
#
#     def get_transcript(self, url: str) -> Optional[str]:
#
#         try:
#             video_id = self.extract_youtube_video_id(url)
#
#             if not video_id:
#                 logger.warning(f"Could not extract video ID from URL: {url}")
#                 return None
#
#             api = YouTubeTranscriptApi()
#
#             transcript = api.fetch(video_id, languages=["en"])
#
#             formatted_transcript = "\n".join([snippet.text for snippet in transcript])
#
#             return formatted_transcript
#
#         except TranscriptsDisabled:
#             logger.warning(f"Transcripts are disabled for video: {video_id}")
#             return "disabled"
#         except NoTranscriptFound:
#             logger.warning(f"No transcript found for video: {video_id}")
#             return "not_found"
#         except Exception as e:
#             logger.error(f"Error fetching transcript: {e}")
#             return None
#
#     async def summarize_transcript(self, transcript: str) -> Optional[str]:
#
#         loop = asyncio.get_event_loop()
#         return await loop.run_in_executor(executor, self._execute_summarize, transcript)
#
#     def _execute_summarize(self, transcript: str) -> Optional[str]:
#         try:
#             if not transcript or len(transcript.strip()) == 0:
#                 return None
#
#             model = self._load_summarizer()
#
#             device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
#
#             max_chunk_length = 600
#
#             if len(transcript) > max_chunk_length:
#                 sentences = transcript.split('. ')
#                 chunks, current_chunk = [], ""
#
#                 for sentence in sentences:
#                     if len(current_chunk) + len(sentence) < max_chunk_length:
#                         current_chunk += sentence + ". "
#                     else:
#                         if current_chunk:
#                             chunks.append(current_chunk.strip())
#                         current_chunk = sentence + ". "
#                 if current_chunk:
#                     chunks.append(current_chunk.strip())
#
#                 summaries = []
#                 for i, chunk in enumerate(chunks):
#                     try:
#                         logger.info(f"Summarizing chunk {i + 1}/{len(chunks)}")
#
#                         prompt = f"""Create a comprehensive, well-structured summary of this transcript section.
#
#                         Instructions:
#                         - Organize into sections with clear, descriptive headers
#                         - Under each header, use bullet points to explain key information
#                         - Preserve all important details: names, numbers, specific terms, and reasoning
#                         - Maintain the original flow and structure of the content
#                         - Be thorough - include context and explanations
#                         - Keep tone neutral and factual
#
#                         Transcript section:
#                         {chunk}
#                         """
#
#                         inputs = self.tokenizer(prompt, return_tensors="pt", truncation=True, max_length=512).to(device)
#
#                         with torch.no_grad():
#                             outputs = model.generate(
#                                 **inputs,
#                                 max_new_tokens=2000,
#                                 temperature=0.7,
#                                 top_p=0.9,
#                                 do_sample=True
#                             )
#
#                         summary = self.tokenizer.decode(outputs[0], skip_special_tokens=True)
#                         summaries.append(summary)
#
#                     except Exception as e:
#                         logger.warning(f"Error summarizing chunk {i + 1}: {e}")
#                         continue
#
#                 if not summaries:
#                     return None
#
#                 combined_text = "\n\n".join(summaries)
#                 synthesis_prompt = f"""Combine these partial summaries into one cohesive, well-structured summary.
#
#                 Instructions:
#                 - Merge related sections and eliminate redundancy
#                 - Maintain the same format: descriptive headers with bullet points
#                 - Preserve all key details from the partial summaries
#                 - Create a logical flow from start to finish
#                 - Be thorough and complete
#
#                 Partial summaries to combine:
#                 {combined_text}
#                 """
#
#                 inputs = self.tokenizer(synthesis_prompt, return_tensors="pt", truncation=True, max_length=512).to(
#                     device)
#                 with torch.no_grad():
#                     outputs = model.generate(
#                         **inputs,
#                         max_new_tokens=500,
#                         temperature=0.7,
#                         top_p=0.9,
#                         do_sample=True
#                     )
#
#                 final_summary = self.tokenizer.decode(outputs[0], skip_special_tokens=True)
#                 return final_summary.strip()
#
#             else:
#                 prompt = f"""Create a comprehensive, well-structured summary of this transcript.
#
#                 Instructions:
#                 - Organize into logical sections with descriptive headers (e.g., "Introduction to Topic", "Main Section: Specific Element")
#                 - Under each header, use bullet points to explain key information
#                 - Preserve all important details: names, numbers, specific terms, and reasoning
#                 - Maintain the original flow and structure of the content
#                 - Be thorough - include context and explanations, not just bare facts
#                 - Keep tone neutral and factual
#
#                 Transcript:
#                 {transcript}
#                 """
#
#                 inputs = self.tokenizer(prompt, return_tensors="pt", truncation=True, max_length=512).to(device)
#                 with torch.no_grad():
#                     outputs = model.generate(
#                         **inputs,
#                         max_new_tokens=2000,
#                         temperature=0.7,
#                         top_p=0.9,
#                         do_sample=True
#                     )
#
#                 summary = self.tokenizer.decode(outputs[0], skip_special_tokens=True)
#                 return summary.strip()
#
#         except Exception as e:
#             logger.error(f"Error summarizing transcript: {e}")
#             return None
