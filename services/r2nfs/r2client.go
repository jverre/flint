package main

import (
	"bytes"
	"context"
	"fmt"
	"io"
	"strings"
	"time"

	"github.com/aws/aws-sdk-go-v2/aws"
	"github.com/aws/aws-sdk-go-v2/credentials"
	"github.com/aws/aws-sdk-go-v2/service/s3"
	"github.com/aws/aws-sdk-go-v2/service/s3/types"
)

// R2Client wraps the S3-compatible API for Cloudflare R2.
type R2Client struct {
	client *s3.Client
	bucket string
}

// R2Object holds metadata and optional content for an R2 object.
type R2Object struct {
	Key          string
	Size         int64
	LastModified time.Time
	IsPrefix     bool // true for "directory" prefixes
}

func newR2Client(endpoint, accessKey, secretKey, bucket string) (*R2Client, error) {
	client := s3.New(s3.Options{
		BaseEndpoint: &endpoint,
		Region:       "auto",
		Credentials:  credentials.NewStaticCredentialsProvider(accessKey, secretKey, ""),
		UsePathStyle: true,
	})
	return &R2Client{client: client, bucket: bucket}, nil
}

// GetObject downloads an object from R2.
func (r *R2Client) GetObject(ctx context.Context, key string) ([]byte, error) {
	out, err := r.client.GetObject(ctx, &s3.GetObjectInput{
		Bucket: &r.bucket,
		Key:    &key,
	})
	if err != nil {
		return nil, err
	}
	defer out.Body.Close()
	return io.ReadAll(out.Body)
}

// PutObject uploads an object to R2.
func (r *R2Client) PutObject(ctx context.Context, key string, data []byte) error {
	_, err := r.client.PutObject(ctx, &s3.PutObjectInput{
		Bucket: &r.bucket,
		Key:    &key,
		Body:   bytes.NewReader(data),
	})
	return err
}

// HeadObject returns metadata for an object. Returns nil if not found.
func (r *R2Client) HeadObject(ctx context.Context, key string) (*R2Object, error) {
	out, err := r.client.HeadObject(ctx, &s3.HeadObjectInput{
		Bucket: &r.bucket,
		Key:    &key,
	})
	if err != nil {
		return nil, err
	}
	obj := &R2Object{
		Key:  key,
		Size: aws.ToInt64(out.ContentLength),
	}
	if out.LastModified != nil {
		obj.LastModified = *out.LastModified
	}
	return obj, nil
}

// DeleteObject removes an object from R2.
func (r *R2Client) DeleteObject(ctx context.Context, key string) error {
	_, err := r.client.DeleteObject(ctx, &s3.DeleteObjectInput{
		Bucket: &r.bucket,
		Key:    &key,
	})
	return err
}

// CopyObject copies an object within the same bucket.
func (r *R2Client) CopyObject(ctx context.Context, srcKey, dstKey string) error {
	copySource := fmt.Sprintf("%s/%s", r.bucket, srcKey)
	_, err := r.client.CopyObject(ctx, &s3.CopyObjectInput{
		Bucket:     &r.bucket,
		CopySource: &copySource,
		Key:        &dstKey,
	})
	return err
}

// ListObjects lists objects under a prefix. Returns both objects and common prefixes (directories).
func (r *R2Client) ListObjects(ctx context.Context, prefix string) ([]R2Object, error) {
	delimiter := "/"
	out, err := r.client.ListObjectsV2(ctx, &s3.ListObjectsV2Input{
		Bucket:    &r.bucket,
		Prefix:    &prefix,
		Delimiter: &delimiter,
	})
	if err != nil {
		return nil, err
	}

	var result []R2Object

	// Common prefixes are "directories".
	for _, p := range out.CommonPrefixes {
		name := aws.ToString(p.Prefix)
		// Strip the parent prefix to get the directory name.
		name = strings.TrimPrefix(name, prefix)
		name = strings.TrimSuffix(name, "/")
		if name != "" {
			result = append(result, R2Object{Key: name, IsPrefix: true})
		}
	}

	// Objects are "files".
	for _, obj := range out.Contents {
		key := aws.ToString(obj.Key)
		name := strings.TrimPrefix(key, prefix)
		// Skip empty names (the prefix itself) and whiteout markers in listing.
		if name == "" {
			continue
		}
		entry := R2Object{
			Key:  name,
			Size: aws.ToInt64(obj.Size),
		}
		if obj.LastModified != nil {
			entry.LastModified = *obj.LastModified
		}
		result = append(result, entry)
	}

	return result, nil
}

// DeletePrefix removes all objects under a prefix (recursive delete).
func (r *R2Client) DeletePrefix(ctx context.Context, prefix string) error {
	paginator := s3.NewListObjectsV2Paginator(r.client, &s3.ListObjectsV2Input{
		Bucket: &r.bucket,
		Prefix: &prefix,
	})

	for paginator.HasMorePages() {
		page, err := paginator.NextPage(ctx)
		if err != nil {
			return err
		}
		if len(page.Contents) == 0 {
			continue
		}
		var objects []types.ObjectIdentifier
		for _, obj := range page.Contents {
			objects = append(objects, types.ObjectIdentifier{Key: obj.Key})
		}
		_, err = r.client.DeleteObjects(ctx, &s3.DeleteObjectsInput{
			Bucket: &r.bucket,
			Delete: &types.Delete{Objects: objects},
		})
		if err != nil {
			return err
		}
	}
	return nil
}
