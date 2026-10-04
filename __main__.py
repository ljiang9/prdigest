try:
    from .prdigest import main
except ImportError:  # python __main__.py 直接运行
    from prdigest import main

if __name__ == "__main__":
    main()
