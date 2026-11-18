import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

/** Rendered skill output. Raw HTML in the Markdown is not rendered (react-markdown default). */
export function Markdown({ children }: { children: string }) {
  return (
    <div className="prose prose-slate prose-sm max-w-none prose-headings:mt-5 prose-headings:mb-2 prose-li:my-0.5">
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        components={{
          a: ({ href, children: c }) => (
            <a href={href} target="_blank" rel="noopener noreferrer nofollow">
              {c}
            </a>
          ),
        }}
      >
        {children}
      </ReactMarkdown>
    </div>
  );
}
