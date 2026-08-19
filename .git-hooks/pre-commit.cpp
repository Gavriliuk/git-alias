#define WIN32_LEAN_AND_MEAN
#include <windows.h>

#include <cstdio>
#include <cstdlib>
#include <fcntl.h>
#include <io.h>
#include <algorithm>
#include <cctype>
#include <cstdint>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <iterator>
#include <set>
#include <stdexcept>
#include <string>
#include <thread>
#include <unordered_map>
#include <utility>
#include <vector>

namespace fs = std::filesystem;

struct ProcessResult
{
    DWORD exit_code = 0;
    std::string out;
    std::string err;
};

struct Line
{
    std::string body;
    std::string eol;
};

struct Hunk
{
    int old_start = 0;
    int old_count = 0;
    int new_start = 0;
    int new_count = 0;
};

struct IndexEntry
{
    std::string mode;
    std::string oid;
    int stage = 0;
};

struct Plan
{
    std::string path;
    std::string mode;
    std::string old_oid;
    std::string new_index;
    std::string worktree_old;
    std::string worktree_new;
    std::vector<int> changed_lines;
    bool write_worktree = false;
};

static std::runtime_error win32_error(const char* what)
{
    return std::runtime_error(
        std::string(what) + " failed (Win32 error " +
        std::to_string(GetLastError()) + ")"
    );
}

static std::wstring quote_arg(const std::wstring& arg)
{
    if (arg.empty())
        return L"\"\"";

    if (arg.find_first_of(L" \t\n\v\"") == std::wstring::npos)
        return arg;

    std::wstring result = L"\"";
    size_t backslashes = 0;

    for (wchar_t ch : arg)
    {
        if (ch == L'\\')
        {
            ++backslashes;
            continue;
        }

        if (ch == L'\"')
        {
            result.append(backslashes * 2 + 1, L'\\');
            result.push_back(L'\"');
            backslashes = 0;
            continue;
        }

        result.append(backslashes, L'\\');
        backslashes = 0;
        result.push_back(ch);
    }

    result.append(backslashes * 2, L'\\');
    result.push_back(L'\"');
    return result;
}

static void read_handle(HANDLE handle, std::string& output)
{
    char buffer[65536];
    DWORD read = 0;

    while (ReadFile(handle, buffer, sizeof(buffer), &read, nullptr) && read != 0)
        output.append(buffer, buffer + read);

    CloseHandle(handle);
}

static void write_handle(HANDLE handle, const std::string& input)
{
    size_t offset = 0;

    while (offset < input.size())
    {
        DWORD chunk = static_cast<DWORD>(
            std::min<size_t>(input.size() - offset, 0x7fffffffU)
        );
        DWORD written = 0;

        if (!WriteFile(handle, input.data() + offset, chunk, &written, nullptr))
        {
            CloseHandle(handle);
            return;
        }

        offset += written;
    }

    CloseHandle(handle);
}

static ProcessResult run_process(
    const std::vector<std::wstring>& args,
    const std::string& input = std::string()
)
{
    if (args.empty())
        throw std::runtime_error("empty process command");

    SECURITY_ATTRIBUTES sa{};
    sa.nLength = sizeof(sa);
    sa.bInheritHandle = TRUE;

    HANDLE in_read = nullptr;
    HANDLE in_write = nullptr;
    HANDLE out_read = nullptr;
    HANDLE out_write = nullptr;
    HANDLE err_read = nullptr;
    HANDLE err_write = nullptr;

    if (!CreatePipe(&in_read, &in_write, &sa, 0))
        throw win32_error("CreatePipe(stdin)");
    if (!CreatePipe(&out_read, &out_write, &sa, 0))
        throw win32_error("CreatePipe(stdout)");
    if (!CreatePipe(&err_read, &err_write, &sa, 0))
        throw win32_error("CreatePipe(stderr)");

    SetHandleInformation(in_write, HANDLE_FLAG_INHERIT, 0);
    SetHandleInformation(out_read, HANDLE_FLAG_INHERIT, 0);
    SetHandleInformation(err_read, HANDLE_FLAG_INHERIT, 0);

    STARTUPINFOW si{};
    si.cb = sizeof(si);
    si.dwFlags = STARTF_USESTDHANDLES;
    si.hStdInput = in_read;
    si.hStdOutput = out_write;
    si.hStdError = err_write;

    PROCESS_INFORMATION pi{};

    std::wstring command_line;
    for (size_t i = 0; i < args.size(); ++i)
    {
        if (i != 0)
            command_line.push_back(L' ');
        command_line += quote_arg(args[i]);
    }

    std::vector<wchar_t> mutable_command(command_line.begin(), command_line.end());
    mutable_command.push_back(L'\0');

    BOOL ok = CreateProcessW(
        nullptr,
        mutable_command.data(),
        nullptr,
        nullptr,
        TRUE,
        CREATE_NO_WINDOW,
        nullptr,
        nullptr,
        &si,
        &pi
    );

    CloseHandle(in_read);
    CloseHandle(out_write);
    CloseHandle(err_write);

    if (!ok)
    {
        CloseHandle(in_write);
        CloseHandle(out_read);
        CloseHandle(err_read);
        throw win32_error("CreateProcess");
    }

    ProcessResult result;

    std::thread stdout_thread(read_handle, out_read, std::ref(result.out));
    std::thread stderr_thread(read_handle, err_read, std::ref(result.err));
    std::thread stdin_thread(write_handle, in_write, std::cref(input));

    WaitForSingleObject(pi.hProcess, INFINITE);

    stdin_thread.join();
    stdout_thread.join();
    stderr_thread.join();

    if (!GetExitCodeProcess(pi.hProcess, &result.exit_code))
    {
        CloseHandle(pi.hThread);
        CloseHandle(pi.hProcess);
        throw win32_error("GetExitCodeProcess");
    }

    CloseHandle(pi.hThread);
    CloseHandle(pi.hProcess);
    return result;
}

static ProcessResult git(
    const std::vector<std::wstring>& args,
    const std::string& input = std::string(),
    bool check = true
)
{
    std::vector<std::wstring> command;
    command.reserve(args.size() + 1);
    command.push_back(L"git");
    command.insert(command.end(), args.begin(), args.end());

    ProcessResult result = run_process(command, input);

    if (check && result.exit_code != 0)
    {
        if (!result.out.empty())
            std::cout.write(result.out.data(), static_cast<std::streamsize>(result.out.size()));
        if (!result.err.empty())
            std::cerr.write(result.err.data(), static_cast<std::streamsize>(result.err.size()));

        throw std::runtime_error("git command failed (" + std::to_string(result.exit_code) + ")");
    }

    return result;
}

static std::string trim_eol(std::string value)
{
    while (!value.empty() && (value.back() == '\r' || value.back() == '\n'))
        value.pop_back();
    return value;
}

static bool is_source(const std::string& path)
{
    static const std::set<std::string> extensions = {
        ".c", ".cc", ".cpp", ".cxx",
        ".h", ".hh", ".hpp", ".hxx",
        ".inl", ".nsh", ".nsi", ".rc",
    };

    std::string ext = fs::u8path(path).extension().u8string();
    std::transform(ext.begin(), ext.end(), ext.begin(), [](unsigned char ch) {
        return static_cast<char>(std::tolower(ch));
    });

    return extensions.find(ext) != extensions.end();
}

static std::vector<Line> split_lines(const std::string& data)
{
    std::vector<Line> lines;
    size_t start = 0;
    size_t i = 0;

    while (i < data.size())
    {
        if (data[i] == '\r')
        {
            if (i + 1 < data.size() && data[i + 1] == '\n')
            {
                lines.push_back({data.substr(start, i - start), "\r\n"});
                i += 2;
            }
            else
            {
                lines.push_back({data.substr(start, i - start), "\r"});
                ++i;
            }
            start = i;
        }
        else if (data[i] == '\n')
        {
            lines.push_back({data.substr(start, i - start), "\n"});
            ++i;
            start = i;
        }
        else
        {
            ++i;
        }
    }

    if (start < data.size())
        lines.push_back({data.substr(start), ""});

    return lines;
}

static std::string join_lines(const std::vector<Line>& lines)
{
    size_t total = 0;
    for (const Line& line : lines)
        total += line.body.size() + line.eol.size();

    std::string result;
    result.reserve(total);

    for (const Line& line : lines)
    {
        result += line.body;
        result += line.eol;
    }

    return result;
}

static Line normalize_line(const Line& line)
{
    std::string body = line.body;

    while (!body.empty() && (body.back() == ' ' || body.back() == '\t'))
        body.pop_back();

    size_t pos = 0;
    while (pos < body.size() && (body[pos] == ' ' || body[pos] == '\t'))
        ++pos;

    for (size_t i = 0; i < pos; ++i)
    {
        if (body[i] == '\t')
            body[i] = ' ';
    }

    return {std::move(body), "\r\n"};
}

static std::string normalize_blob(const std::string& data)
{
    std::vector<Line> lines = split_lines(data);

    for (Line& line : lines)
        line = normalize_line(line);

    return join_lines(lines);
}

static bool line_equal(const Line& a, const Line& b)
{
    return a.body == b.body && a.eol == b.eol;
}

static std::vector<std::string> split_nul(const std::string& data)
{
    std::vector<std::string> result;
    size_t start = 0;

    while (start < data.size())
    {
        size_t end = data.find('\0', start);
        if (end == std::string::npos)
            end = data.size();

        if (end != start)
            result.push_back(data.substr(start, end - start));

        start = end + 1;
    }

    return result;
}

static bool parse_number(const std::string& text, size_t& pos, int& value)
{
    if (pos >= text.size() || !std::isdigit(static_cast<unsigned char>(text[pos])))
        return false;

    int number = 0;
    while (pos < text.size() && std::isdigit(static_cast<unsigned char>(text[pos])))
    {
        number = number * 10 + (text[pos] - '0');
        ++pos;
    }

    value = number;
    return true;
}

static bool parse_hunk_header(const std::string& line, Hunk& hunk)
{
    if (line.rfind("@@ -", 0) != 0)
        return false;

    size_t pos = 4;
    if (!parse_number(line, pos, hunk.old_start))
        return false;

    hunk.old_count = 1;
    if (pos < line.size() && line[pos] == ',')
    {
        ++pos;
        if (!parse_number(line, pos, hunk.old_count))
            return false;
    }

    if (pos >= line.size() || line[pos] != ' ')
        return false;
    ++pos;

    if (pos >= line.size() || line[pos] != '+')
        return false;
    ++pos;

    if (!parse_number(line, pos, hunk.new_start))
        return false;

    hunk.new_count = 1;
    if (pos < line.size() && line[pos] == ',')
    {
        ++pos;
        if (!parse_number(line, pos, hunk.new_count))
            return false;
    }

    return true;
}

static std::string git_unquote_path(const std::string& value)
{
    if (value.size() < 2 || value.front() != '"' || value.back() != '"')
        return value;

    std::string result;

    for (size_t i = 1; i + 1 < value.size(); ++i)
    {
        unsigned char ch = static_cast<unsigned char>(value[i]);

        if (ch != '\\')
        {
            result.push_back(static_cast<char>(ch));
            continue;
        }

        if (++i + 1 > value.size())
            break;

        ch = static_cast<unsigned char>(value[i]);
        switch (ch)
        {
            case '\\': result.push_back('\\'); break;
            case '"':  result.push_back('"');  break;
            case 'a':  result.push_back('\a'); break;
            case 'b':  result.push_back('\b'); break;
            case 'f':  result.push_back('\f'); break;
            case 'n':  result.push_back('\n'); break;
            case 'r':  result.push_back('\r'); break;
            case 't':  result.push_back('\t'); break;
            case 'v':  result.push_back('\v'); break;

            default:
                if (ch >= '0' && ch <= '7')
                {
                    int octal = ch - '0';
                    int count = 1;
                    while (count < 3 && i + 1 < value.size() - 1 &&
                           value[i + 1] >= '0' && value[i + 1] <= '7')
                    {
                        ++i;
                        octal = octal * 8 + (value[i] - '0');
                        ++count;
                    }
                    result.push_back(static_cast<char>(octal));
                }
                else
                {
                    result.push_back(static_cast<char>(ch));
                }
                break;
        }
    }

    return result;
}

static std::string patch_path(std::string value)
{
    if (!value.empty() && value.back() == '\r')
        value.pop_back();

    if (!value.empty() && value.back() == '\t')
        value.pop_back();

    value = git_unquote_path(value);

    if (value == "/dev/null")
        return std::string();

    if (value.rfind("b/", 0) == 0)
        value.erase(0, 2);

    return value;
}

static std::unordered_map<std::string, std::vector<Hunk>> parse_patch(const std::string& patch)
{
    std::unordered_map<std::string, std::vector<Hunk>> result;
    std::string current_path;
    bool expect_new_path = false;
    size_t start = 0;

    while (start <= patch.size())
    {
        size_t end = patch.find('\n', start);
        if (end == std::string::npos)
            end = patch.size();

        std::string line = patch.substr(start, end - start);

        if (line.rfind("--- ", 0) == 0)
        {
            expect_new_path = true;
        }
        else if (expect_new_path && line.rfind("+++ ", 0) == 0)
        {
            current_path = patch_path(line.substr(4));
            expect_new_path = false;
        }
        else
        {
            expect_new_path = false;

            if (!current_path.empty())
            {
                Hunk hunk;
                if (parse_hunk_header(line, hunk))
                    result[current_path].push_back(hunk);
            }
        }

        if (end == patch.size())
            break;
        start = end + 1;
    }

    return result;
}

static std::set<int> staged_line_numbers(const std::vector<Hunk>& hunks)
{
    std::set<int> result;

    for (const Hunk& hunk : hunks)
    {
        for (int i = 0; i < hunk.new_count; ++i)
            result.insert(hunk.new_start + i);
    }

    return result;
}

static int map_index_line_to_worktree(int line_no, const std::vector<Hunk>& hunks)
{
    int delta = 0;

    for (const Hunk& hunk : hunks)
    {
        if (hunk.old_count == 0)
        {
            if (line_no <= hunk.old_start)
                return line_no + delta;

            delta += hunk.new_count;
            continue;
        }

        if (line_no < hunk.old_start)
            return line_no + delta;

        if (line_no < hunk.old_start + hunk.old_count)
            return 0;

        delta += hunk.new_count - hunk.old_count;
    }

    return line_no + delta;
}

static std::unordered_map<std::string, IndexEntry> parse_index_entries(
    const std::string& raw,
    bool& has_unmerged
)
{
    std::unordered_map<std::string, IndexEntry> result;
    has_unmerged = false;

    for (const std::string& entry : split_nul(raw))
    {
        size_t tab = entry.find('\t');
        if (tab == std::string::npos)
            continue;

        std::string metadata = entry.substr(0, tab);
        std::string path = entry.substr(tab + 1);

        size_t p1 = metadata.find(' ');
        size_t p2 = metadata.find(' ', p1 == std::string::npos ? p1 : p1 + 1);

        if (p1 == std::string::npos || p2 == std::string::npos)
            continue;

        IndexEntry parsed;
        parsed.mode = metadata.substr(0, p1);
        parsed.oid = metadata.substr(p1 + 1, p2 - p1 - 1);
        parsed.stage = std::stoi(metadata.substr(p2 + 1));

        if (parsed.stage != 0)
        {
            has_unmerged = true;
            continue;
        }

        result.emplace(std::move(path), std::move(parsed));
    }

    return result;
}


struct ChangedPath
{
    std::string path;
    char status = 0;
};

static void append_source_pathspecs(std::vector<std::wstring>& args)
{
    static const wchar_t* extensions[] = {
        L"c", L"cc", L"cpp", L"cxx",
        L"h", L"hh", L"hpp", L"hxx",
        L"inl", L"nsh", L"nsi", L"rc",
    };

    for (const wchar_t* ext : extensions)
        args.push_back(std::wstring(L":(icase,glob)**/*.") + ext);
}

static std::vector<ChangedPath> parse_name_status(const std::string& raw)
{
    std::vector<std::string> fields = split_nul(raw);
    std::vector<ChangedPath> result;
    size_t i = 0;

    while (i < fields.size())
    {
        std::string status_text = fields[i++];
        if (status_text.empty())
            continue;

        char status = status_text[0];
        std::string path;

        if (status == 'R' || status == 'C')
        {
            if (i + 1 >= fields.size())
                throw std::runtime_error("invalid git diff --name-status output");

            ++i; // Old path.
            path = fields[i++];
        }
        else
        {
            if (i >= fields.size())
                throw std::runtime_error("invalid git diff --name-status output");

            path = fields[i++];
        }

        if (is_source(path))
            result.push_back({std::move(path), status});
    }

    return result;
}

static std::vector<std::string> cat_file_batch(const std::vector<std::string>& oids)
{
    std::string input;
    for (const std::string& oid : oids)
    {
        input += oid;
        input.push_back('\n');
    }

    ProcessResult response = git({L"cat-file", L"--batch"}, input);
    std::vector<std::string> blobs;
    blobs.reserve(oids.size());

    size_t pos = 0;
    for (size_t i = 0; i < oids.size(); ++i)
    {
        size_t header_end = response.out.find('\n', pos);
        if (header_end == std::string::npos)
            throw std::runtime_error("invalid git cat-file --batch response");

        std::string header = response.out.substr(pos, header_end - pos);
        size_t last_space = header.rfind(' ');
        size_t second_last_space = last_space == std::string::npos
            ? std::string::npos
            : header.rfind(' ', last_space - 1);

        if (last_space == std::string::npos || second_last_space == std::string::npos ||
            header.substr(second_last_space + 1, last_space - second_last_space - 1) != "blob")
        {
            throw std::runtime_error("expected blob from git cat-file --batch");
        }

        uint64_t size = std::stoull(header.substr(last_space + 1));
        pos = header_end + 1;

        if (size > response.out.size() - pos)
            throw std::runtime_error("truncated git cat-file --batch response");

        blobs.emplace_back(response.out.data() + pos, static_cast<size_t>(size));
        pos += static_cast<size_t>(size);

        if (pos >= response.out.size() || response.out[pos] != '\n')
            throw std::runtime_error("invalid git cat-file --batch separator");
        ++pos;
    }

    return blobs;
}

static std::string read_file(const fs::path& path)
{
    std::ifstream stream(path, std::ios::binary);
    if (!stream)
        throw std::runtime_error("cannot read file: " + path.u8string());

    stream.seekg(0, std::ios::end);
    std::streamoff size = stream.tellg();
    stream.seekg(0, std::ios::beg);

    std::string data(static_cast<size_t>(size), '\0');
    if (size > 0)
        stream.read(&data[0], size);

    if (!stream && size > 0)
        throw std::runtime_error("cannot read file: " + path.u8string());

    return data;
}

static void write_file(const fs::path& path, const std::string& data)
{
    std::ofstream stream(path, std::ios::binary | std::ios::trunc);
    if (!stream)
        throw std::runtime_error("cannot write file: " + path.u8string());

    if (!data.empty())
        stream.write(data.data(), static_cast<std::streamsize>(data.size()));

    if (!stream)
        throw std::runtime_error("cannot write file: " + path.u8string());
}

class TempDir
{
public:
    TempDir()
    {
        path_ = fs::temp_directory_path() /
            (L"git-pre-commit-" + std::to_wstring(GetCurrentProcessId()) + L"-" +
             std::to_wstring(GetTickCount64()));
        fs::create_directories(path_);
    }

    ~TempDir()
    {
        std::error_code ec;
        fs::remove_all(path_, ec);
    }

    const fs::path& path() const { return path_; }

private:
    fs::path path_;
};

static std::vector<std::string> hash_blobs(const std::vector<Plan>& plans)
{
    TempDir temp;
    std::string input;

    for (size_t i = 0; i < plans.size(); ++i)
    {
        fs::path path = temp.path() / (L"blob-" + std::to_wstring(i) + L".tmp");
        write_file(path, plans[i].new_index);

        input += path.u8string();
        input.push_back('\n');
    }

    ProcessResult response = git(
        {L"hash-object", L"-w", L"--stdin-paths"},
        input
    );

    std::vector<std::string> oids;
    size_t start = 0;

    while (start < response.out.size())
    {
        size_t end = response.out.find('\n', start);
        if (end == std::string::npos)
            end = response.out.size();

        std::string oid = response.out.substr(start, end - start);
        if (!oid.empty() && oid.back() == '\r')
            oid.pop_back();
        if (!oid.empty())
            oids.push_back(std::move(oid));

        start = end + 1;
    }

    if (oids.size() != plans.size())
        throw std::runtime_error("git hash-object returned an unexpected number of object ids");

    return oids;
}

static void update_index(
    const std::vector<Plan>& plans,
    const std::vector<std::string>& oids
)
{
    std::string input;

    for (size_t i = 0; i < plans.size(); ++i)
    {
        input += plans[i].mode;
        input += " blob ";
        input += oids[i];
        input.push_back('\t');
        input += plans[i].path;
        input.push_back('\0');
    }

    git({L"update-index", L"-z", L"--index-info"}, input);
}

static void rollback_index(const std::vector<Plan>& plans)
{
    std::vector<std::string> old_oids;
    old_oids.reserve(plans.size());
    for (const Plan& plan : plans)
        old_oids.push_back(plan.old_oid);

    try
    {
        update_index(plans, old_oids);
    }
    catch (...)
    {
    }
}

static int run_pre_commit()
{
    std::string root_text = trim_eol(git({L"rev-parse", L"--show-toplevel"}).out);
    fs::current_path(fs::u8path(root_text));

    std::vector<std::wstring> names_args = {
        L"diff", L"--cached", L"--name-status",
        L"--diff-filter=ACMR", L"-z", L"--"
    };
    append_source_pathspecs(names_args);

    std::vector<ChangedPath> changed_paths =
        parse_name_status(git(names_args).out);

    if (changed_paths.empty())
        return 0;

    std::vector<std::string> paths;
    paths.reserve(changed_paths.size());
    for (const ChangedPath& changed : changed_paths)
        paths.push_back(changed.path);

    ProcessResult index_result = git({L"ls-files", L"--stage", L"-z"});
    bool has_unmerged = false;
    auto index_entries = parse_index_entries(index_result.out, has_unmerged);

    if (has_unmerged)
    {
        std::cerr << "pre-commit: unresolved merge entries; refusing to normalize\n";
        return 1;
    }

    // Disable textconv explicitly. The repository has diff drivers using
    // iconv, but this hook only needs raw line numbers, not converted text.
    std::vector<std::wstring> staged_patch_args = {
        L"-c", L"core.quotePath=false",
        L"diff", L"--cached", L"--unified=0", L"--diff-filter=MCR",
        L"--no-color", L"--no-ext-diff", L"--no-textconv", L"--"
    };
    append_source_pathspecs(staged_patch_args);
    ProcessResult staged_patch_result = git(staged_patch_args);

    std::vector<std::wstring> worktree_patch_args = {
        L"-c", L"core.quotePath=false",
        L"diff", L"--unified=0",
        L"--no-color", L"--no-ext-diff", L"--no-textconv", L"--"
    };
    append_source_pathspecs(worktree_patch_args);
    ProcessResult worktree_patch_result = git(worktree_patch_args);

    auto staged_hunks = parse_patch(staged_patch_result.out);
    auto worktree_hunks = parse_patch(worktree_patch_result.out);

    std::vector<std::string> blob_oids;
    blob_oids.reserve(paths.size());

    for (const std::string& path : paths)
    {
        auto entry = index_entries.find(path);
        if (entry == index_entries.end())
            throw std::runtime_error("expected one index entry for " + path);

        blob_oids.push_back(entry->second.oid);
    }

    std::vector<std::string> blobs = cat_file_batch(blob_oids);
    std::vector<Plan> plans;

    // Build the complete plan before changing either index or working tree.
    for (size_t path_index = 0; path_index < paths.size(); ++path_index)
    {
        const std::string& path = paths[path_index];
        const IndexEntry& entry = index_entries.at(path);

        std::vector<Line> index_lines = split_lines(blobs[path_index]);
        std::set<int> targets;

        if (changed_paths[path_index].status == 'A')
        {
            for (size_t i = 0; i < index_lines.size(); ++i)
                targets.insert(static_cast<int>(i + 1));
        }
        else
        {
            auto target_hunks = staged_hunks.find(path);
            if (target_hunks != staged_hunks.end())
                targets = staged_line_numbers(target_hunks->second);
        }

        std::vector<int> changed_lines;
        std::vector<Line> new_index_lines = index_lines;

        for (int line_no : targets)
        {
            if (line_no < 1 || static_cast<size_t>(line_no) > index_lines.size())
                continue;

            const Line& old_line = index_lines[static_cast<size_t>(line_no - 1)];
            Line new_line = normalize_line(old_line);

            if (!line_equal(new_line, old_line))
            {
                new_index_lines[static_cast<size_t>(line_no - 1)] = std::move(new_line);
                changed_lines.push_back(line_no);
            }
        }

        if (changed_lines.empty())
            continue;

        Plan plan;
        plan.path = path;
        plan.mode = entry.mode;
        plan.old_oid = entry.oid;
        plan.new_index = join_lines(new_index_lines);
        plan.changed_lines = changed_lines;

        fs::path physical_path = fs::u8path(path);
        std::error_code file_error;

        if (fs::is_regular_file(physical_path, file_error) && !file_error)
        {
            plan.worktree_old = read_file(physical_path);
            std::vector<Line> worktree_lines = split_lines(plan.worktree_old);
            bool worktree_changed = false;

            static const std::vector<Hunk> no_hunks;
            auto unstaged = worktree_hunks.find(path);
            const std::vector<Hunk>& hunks = unstaged == worktree_hunks.end()
                ? no_hunks
                : unstaged->second;

            for (int line_no : changed_lines)
            {
                int mapped = map_index_line_to_worktree(line_no, hunks);

                if (mapped < 1 || static_cast<size_t>(mapped) > worktree_lines.size())
                    continue;

                Line normalized = normalize_line(worktree_lines[static_cast<size_t>(mapped - 1)]);

                if (!line_equal(normalized, worktree_lines[static_cast<size_t>(mapped - 1)]))
                {
                    worktree_lines[static_cast<size_t>(mapped - 1)] = std::move(normalized);
                    worktree_changed = true;
                }
            }

            if (worktree_changed)
            {
                plan.worktree_new = join_lines(worktree_lines);
                plan.write_worktree = true;
            }
        }

        plans.push_back(std::move(plan));
    }

    if (plans.empty())
        return 0;

    std::vector<std::string> new_oids = hash_blobs(plans);
    std::vector<size_t> written_worktrees;

    try
    {
        update_index(plans, new_oids);

        for (size_t i = 0; i < plans.size(); ++i)
        {
            if (!plans[i].write_worktree)
                continue;

            write_file(fs::u8path(plans[i].path), plans[i].worktree_new);
            written_worktrees.push_back(i);
        }
    }
    catch (...)
    {
        rollback_index(plans);

        for (auto it = written_worktrees.rbegin(); it != written_worktrees.rend(); ++it)
        {
            try
            {
                write_file(fs::u8path(plans[*it].path), plans[*it].worktree_old);
            }
            catch (...)
            {
            }
        }

        throw;
    }

    for (const Plan& plan : plans)
    {
        std::cout << "pre-commit: normalized " << plan.path << ": "
                  << plan.changed_lines.size() << " staged line(s)\n";
    }

    ProcessResult quiet = git(
        {L"diff", L"--cached", L"--quiet"},
        std::string(),
        false
    );

    if (quiet.exit_code == 0)
    {
        std::cout << "pre-commit: normalization removed all staged changes; nothing to commit\n";
        return 1;
    }

    if (quiet.exit_code != 1)
    {
        if (!quiet.err.empty())
            std::cerr.write(quiet.err.data(), static_cast<std::streamsize>(quiet.err.size()));
        throw std::runtime_error("git diff --cached --quiet failed (" +
                                 std::to_string(quiet.exit_code) + ")");
    }

    return 0;
}

static int run_filter()
{
    // Git blobs are binary byte streams. Disable CRT newline translation.
    if (_setmode(_fileno(stdin), _O_BINARY) == -1 ||
        _setmode(_fileno(stdout), _O_BINARY) == -1)
    {
        throw std::runtime_error("cannot switch stdin/stdout to binary mode");
    }

    char* path_buffer = nullptr;
    size_t path_buffer_size = 0;

    if (_dupenv_s(&path_buffer, &path_buffer_size, "PICK_PATH") != 0)
        throw std::runtime_error("cannot read PICK_PATH");

    if (path_buffer == nullptr)
        throw std::runtime_error("PICK_PATH is not set");

    std::string path(path_buffer);
    std::free(path_buffer);

    std::string data
    {
        std::istreambuf_iterator<char>(std::cin),
        std::istreambuf_iterator<char>()
    };

    if (is_source(path))
        data = normalize_blob(data);

    if (!data.empty())
        std::cout.write(data.data(), static_cast<std::streamsize>(data.size()));

    std::cout.flush();
    if (!std::cout)
        throw std::runtime_error("cannot write filtered blob to stdout");

    return 0;
}

int main(int argc, char* argv[])
{
    try
    {
        if (argc == 1)
            return run_pre_commit();

        if (argc == 2 && std::string(argv[1]) == "--filter")
            return run_filter();

        std::cerr << "Usage: pre-commit.exe [--filter]\n";
        return 2;
    }
    catch (const std::exception& error)
    {
        std::cerr << "pre-commit: ERROR: " << error.what() << '\n';
        return 1;
    }
}
