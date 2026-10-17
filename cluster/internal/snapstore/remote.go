// Package snapstore stores VM snapshots (templates, pauses, checkpoints, forks)
// as content-addressed chunks plus a manifest, in an object store shared by the
// cluster and a size-capped cache on every host.
package snapstore

import (
	"bytes"
	"context"
	"errors"
	"fmt"
	"io"
	"net/url"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"time"

	"github.com/aws/aws-sdk-go-v2/aws"
	"github.com/aws/aws-sdk-go-v2/credentials"
	"github.com/aws/aws-sdk-go-v2/service/s3"
	s3types "github.com/aws/aws-sdk-go-v2/service/s3/types"
)

// ErrNotFound is returned by a Remote when a key doesn't exist.
var ErrNotFound = errors.New("not found")

// Remote is the shared object store. Keys are slash-separated.
type Remote interface {
	Put(ctx context.Context, key string, data []byte) error
	Get(ctx context.Context, key string) ([]byte, error)
	Exists(ctx context.Context, key string) (bool, error)
	Delete(ctx context.Context, key string) error
	// List returns every object under prefix.
	List(ctx context.Context, prefix string) ([]Object, error)
}

// Object is one listed key and when it was last written.
type Object struct {
	Key     string
	ModTime time.Time
}

// OpenRemote parses a store URL:
//
//	file:///var/lib/flint/store            a directory (local or a shared mount)
//	s3://bucket/prefix?endpoint=http://...  any S3-compatible store; credentials
//	                                        from AWS_ACCESS_KEY_ID/AWS_SECRET_ACCESS_KEY,
//	                                        region from AWS_REGION (default us-east-1)
func OpenRemote(raw string) (Remote, error) {
	u, err := url.Parse(raw)
	if err != nil {
		return nil, fmt.Errorf("store url: %w", err)
	}
	switch u.Scheme {
	case "file":
		if u.Path == "" {
			return nil, fmt.Errorf("store url %q: missing path", raw)
		}
		return &FileRemote{Root: u.Path}, nil
	case "s3":
		return newS3Remote(u)
	default:
		return nil, fmt.Errorf("store url %q: scheme must be file or s3", raw)
	}
}

// FileRemote keeps objects as files under Root.
type FileRemote struct{ Root string }

func (f *FileRemote) path(key string) (string, error) {
	clean := filepath.Clean("/" + key)
	if clean == "/" || strings.Contains(key, "..") {
		return "", fmt.Errorf("invalid key %q", key)
	}
	return filepath.Join(f.Root, clean), nil
}

func (f *FileRemote) Put(_ context.Context, key string, data []byte) error {
	p, err := f.path(key)
	if err != nil {
		return err
	}
	if err := os.MkdirAll(filepath.Dir(p), 0o755); err != nil {
		return err
	}
	return writeFileAtomic(p, data, 0o644)
}

func (f *FileRemote) Get(_ context.Context, key string) ([]byte, error) {
	p, err := f.path(key)
	if err != nil {
		return nil, err
	}
	b, err := os.ReadFile(p)
	if errors.Is(err, os.ErrNotExist) {
		return nil, ErrNotFound
	}
	return b, err
}

func (f *FileRemote) Exists(_ context.Context, key string) (bool, error) {
	p, err := f.path(key)
	if err != nil {
		return false, err
	}
	_, err = os.Stat(p)
	if errors.Is(err, os.ErrNotExist) {
		return false, nil
	}
	return err == nil, err
}

func (f *FileRemote) Delete(_ context.Context, key string) error {
	p, err := f.path(key)
	if err != nil {
		return err
	}
	err = os.Remove(p)
	if errors.Is(err, os.ErrNotExist) {
		return nil
	}
	return err
}

func (f *FileRemote) List(_ context.Context, prefix string) ([]Object, error) {
	var keys []Object
	base := filepath.Join(f.Root, filepath.Clean("/"+prefix))
	err := filepath.WalkDir(base, func(p string, d os.DirEntry, err error) error {
		if err != nil {
			if errors.Is(err, os.ErrNotExist) {
				return nil
			}
			return err
		}
		if d.IsDir() || strings.Contains(d.Name(), ".tmp-") {
			return nil
		}
		rel, err := filepath.Rel(f.Root, p)
		if err != nil {
			return err
		}
		info, err := d.Info()
		if err != nil {
			return nil // removed while listing
		}
		keys = append(keys, Object{Key: filepath.ToSlash(rel), ModTime: info.ModTime()})
		return nil
	})
	sort.Slice(keys, func(i, j int) bool { return keys[i].Key < keys[j].Key })
	return keys, err
}

type s3Remote struct {
	client *s3.Client
	bucket string
	prefix string
}

func newS3Remote(u *url.URL) (*s3Remote, error) {
	q := u.Query()
	region := q.Get("region")
	if region == "" {
		region = os.Getenv("AWS_REGION")
	}
	if region == "" {
		region = "us-east-1"
	}
	cfg := aws.Config{Region: region}
	if id, secret := os.Getenv("AWS_ACCESS_KEY_ID"), os.Getenv("AWS_SECRET_ACCESS_KEY"); id != "" {
		cfg.Credentials = credentials.NewStaticCredentialsProvider(id, secret, os.Getenv("AWS_SESSION_TOKEN"))
	}
	endpoint := q.Get("endpoint")
	client := s3.NewFromConfig(cfg, func(o *s3.Options) {
		if endpoint != "" {
			o.BaseEndpoint = aws.String(endpoint)
			o.UsePathStyle = true
		}
	})
	if u.Host == "" {
		return nil, fmt.Errorf("s3 store url: missing bucket")
	}
	return &s3Remote{client: client, bucket: u.Host, prefix: strings.Trim(u.Path, "/")}, nil
}

func (s *s3Remote) key(k string) string {
	if s.prefix == "" {
		return k
	}
	return s.prefix + "/" + k
}

func (s *s3Remote) Put(ctx context.Context, key string, data []byte) error {
	_, err := s.client.PutObject(ctx, &s3.PutObjectInput{
		Bucket: &s.bucket, Key: aws.String(s.key(key)), Body: bytes.NewReader(data),
		ContentLength: aws.Int64(int64(len(data))),
	})
	return err
}

func (s *s3Remote) Get(ctx context.Context, key string) ([]byte, error) {
	out, err := s.client.GetObject(ctx, &s3.GetObjectInput{Bucket: &s.bucket, Key: aws.String(s.key(key))})
	if err != nil {
		var nsk *s3types.NoSuchKey
		if errors.As(err, &nsk) || strings.Contains(err.Error(), "StatusCode: 404") {
			return nil, ErrNotFound
		}
		return nil, err
	}
	defer out.Body.Close()
	return io.ReadAll(out.Body)
}

func (s *s3Remote) Exists(ctx context.Context, key string) (bool, error) {
	_, err := s.client.HeadObject(ctx, &s3.HeadObjectInput{Bucket: &s.bucket, Key: aws.String(s.key(key))})
	if err != nil {
		var nf *s3types.NotFound
		if errors.As(err, &nf) || strings.Contains(err.Error(), "StatusCode: 404") {
			return false, nil
		}
		return false, err
	}
	return true, nil
}

func (s *s3Remote) Delete(ctx context.Context, key string) error {
	_, err := s.client.DeleteObject(ctx, &s3.DeleteObjectInput{Bucket: &s.bucket, Key: aws.String(s.key(key))})
	return err
}

func (s *s3Remote) List(ctx context.Context, prefix string) ([]Object, error) {
	var keys []Object
	p := s3.NewListObjectsV2Paginator(s.client, &s3.ListObjectsV2Input{Bucket: &s.bucket, Prefix: aws.String(s.key(prefix))})
	for p.HasMorePages() {
		page, err := p.NextPage(ctx)
		if err != nil {
			return nil, err
		}
		for _, o := range page.Contents {
			k := aws.ToString(o.Key)
			if s.prefix != "" {
				k = strings.TrimPrefix(k, s.prefix+"/")
			}
			keys = append(keys, Object{Key: k, ModTime: aws.ToTime(o.LastModified)})
		}
	}
	return keys, nil
}

func writeFileAtomic(path string, data []byte, mode os.FileMode) error {
	tmp, err := os.CreateTemp(filepath.Dir(path), filepath.Base(path)+".tmp-*")
	if err != nil {
		return err
	}
	if _, err := tmp.Write(data); err != nil {
		tmp.Close()
		os.Remove(tmp.Name())
		return err
	}
	if err := tmp.Chmod(mode); err != nil {
		tmp.Close()
		os.Remove(tmp.Name())
		return err
	}
	if err := tmp.Close(); err != nil {
		os.Remove(tmp.Name())
		return err
	}
	return os.Rename(tmp.Name(), path)
}
